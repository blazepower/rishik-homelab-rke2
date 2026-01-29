package main

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

type Config struct {
	HardcoverAPIKey    string
	BookshelfURL       string
	BookshelfAPIKey    string
	MetadataURL        string // URL for the metadata provider (hardcover.bookinfo.pro or rreading-glasses)
	SyncInterval       int
	DatabasePath       string
}

// Hardcover GraphQL types
type HardcoverResponse struct {
	Data struct {
		Me []struct {
			UserBooks []struct {
				Book struct {
					ID            int    `json:"id"`
					Title         string `json:"title"`
					Contributions []struct {
						Author struct {
							ID   int    `json:"id"`
							Name string `json:"name"`
						} `json:"author"`
					} `json:"contributions"`
					Editions []struct {
						ISBN13 string `json:"isbn_13"`
						ISBN10 string `json:"isbn_10"`
					} `json:"editions"`
				} `json:"book"`
			} `json:"user_books"`
		} `json:"me"`
	} `json:"data"`
	Errors []struct {
		Message string `json:"message"`
	} `json:"errors"`
}

// GraphQL query for fetching Want-To-Read list from Hardcover
// Includes editions with ISBN for better search matching and author ID for cache priming
const hardcoverWantToReadQuery = `{
	"query": "query GetWantToRead { me { user_books(where: {status_id: {_eq: 1}}) { book { id title contributions { author { id name } } editions(limit: 1, order_by: {users_count: desc_nulls_last}) { isbn_13 isbn_10 } } } } }"
}`

func loadConfig() *Config {
	return &Config{
		HardcoverAPIKey: getEnv("HARDCOVER_API_KEY", ""),
		BookshelfURL:    getEnv("BOOKSHELF_URL", "http://bookshelf:8787"),
		BookshelfAPIKey: getEnv("BOOKSHELF_API_KEY", ""),
		MetadataURL:     getEnv("METADATA_URL", "https://hardcover.bookinfo.pro"),
		SyncInterval:    getEnvInt("SYNC_INTERVAL", 3600),
		DatabasePath:    getEnv("DATABASE_PATH", "/data/hardcover-sync.db"),
	}
}

func getEnv(key, defaultValue string) string {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	return value
}

func getEnvInt(key string, defaultValue int) int {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	intValue, err := strconv.Atoi(value)
	if err != nil {
		return defaultValue
	}
	return intValue
}

func initDatabase(dbPath string) (*sql.DB, error) {
	// Ensure directory exists
	dir := filepath.Dir(dbPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create database directory: %w", err)
	}

	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return nil, fmt.Errorf("failed to open database: %w", err)
	}

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS synced_books (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		hardcover_id INTEGER UNIQUE NOT NULL,
		title TEXT NOT NULL,
		synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_hardcover_id ON synced_books(hardcover_id);
	`

	if _, err := db.Exec(createTableSQL); err != nil {
		return nil, fmt.Errorf("failed to create table: %w", err)
	}

	return db, nil
}

func isBookSynced(db *sql.DB, hardcoverID int) (bool, error) {
	var count int
	err := db.QueryRow("SELECT COUNT(*) FROM synced_books WHERE hardcover_id = ?", hardcoverID).Scan(&count)
	if err != nil {
		return false, err
	}
	return count > 0, nil
}

func markBookSynced(db *sql.DB, hardcoverID int, title string) error {
	_, err := db.Exec(
		"INSERT OR IGNORE INTO synced_books (hardcover_id, title) VALUES (?, ?)",
		hardcoverID, title,
	)
	return err
}

// HardcoverBook represents a book from the Hardcover API
type HardcoverBook struct {
	ID       int
	Title    string
	Author   string
	AuthorID int
	ISBN     string
}

func fetchWantToReadList(apiKey string) ([]HardcoverBook, error) {
	req, err := http.NewRequest("POST", "https://api.hardcover.app/v1/graphql", bytes.NewBufferString(hardcoverWantToReadQuery))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	// Handle API key with or without Bearer prefix
	if len(apiKey) > 7 && apiKey[:7] == "Bearer " {
		req.Header.Set("Authorization", apiKey)
	} else {
		req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	}

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch want-to-read list: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("hardcover API returned status %d: %s", resp.StatusCode, string(body))
	}

	var result HardcoverResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	if len(result.Errors) > 0 {
		return nil, fmt.Errorf("hardcover API error: %s", result.Errors[0].Message)
	}

	if len(result.Data.Me) == 0 {
		return nil, fmt.Errorf("no user data returned from Hardcover API")
	}

	books := make([]HardcoverBook, 0, len(result.Data.Me[0].UserBooks))
	for _, ub := range result.Data.Me[0].UserBooks {
		author := ""
		authorID := 0
		if len(ub.Book.Contributions) > 0 {
			author = ub.Book.Contributions[0].Author.Name
			authorID = ub.Book.Contributions[0].Author.ID
		}
		// Extract ISBN - prefer ISBN-13, fallback to ISBN-10
		isbn := ""
		if len(ub.Book.Editions) > 0 {
			if ub.Book.Editions[0].ISBN13 != "" {
				isbn = ub.Book.Editions[0].ISBN13
			} else if ub.Book.Editions[0].ISBN10 != "" {
				isbn = ub.Book.Editions[0].ISBN10
			}
		}
		books = append(books, HardcoverBook{
			ID:       ub.Book.ID,
			Title:    ub.Book.Title,
			Author:   author,
			AuthorID: authorID,
			ISBN:     isbn,
		})
	}

	return books, nil
}

// primeMetadataCache triggers the metadata provider to load work and author data
// by making requests to the work and author endpoints. This causes rreading-glasses
// to fetch the data from Hardcover and cache it for subsequent searches.
// Returns the work data which includes embedded author information.
func primeMetadataCache(metadataURL string, workID int, authorID int) map[string]interface{} {
	client := &http.Client{Timeout: 30 * time.Second}
	var workData map[string]interface{}

	// Prime work cache - this triggers background loading of book data
	// Also captures the work data which includes author information
	if workID > 0 {
		workURL := fmt.Sprintf("%s/work/%d", metadataURL, workID)
		resp, err := client.Get(workURL)
		if err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				if err := json.NewDecoder(resp.Body).Decode(&workData); err == nil {
					log.Printf("Primed metadata cache for work ID %d", workID)
				}
			} else {
				log.Printf("Work lookup returned status %d for ID %d", resp.StatusCode, workID)
			}
		}
	}

	// Prime author cache - this loads all author's books into cache
	if authorID > 0 {
		authorURL := fmt.Sprintf("%s/author/%d", metadataURL, authorID)
		resp, err := client.Get(authorURL)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				log.Printf("Primed metadata cache for author ID %d", authorID)
			}
		}
	}

	// Give the metadata provider time to fetch data from Hardcover
	// rreading-glasses loads data asynchronously in the background
	time.Sleep(2 * time.Second)
	
	return workData
}

func addToBookshelf(bookshelfURL, apiKey, metadataURL string, book HardcoverBook) error {
	// Prime the metadata cache with work and author IDs before searching
	// This triggers rreading-glasses to fetch and cache the data from Hardcover
	// Also returns work data which includes embedded author information
	workData := primeMetadataCache(metadataURL, book.ID, book.AuthorID)

	// First check if book already exists in the library (not lookup/search)
	// Get all books from library and check by title
	libraryURL := fmt.Sprintf("%s/api/v1/book", bookshelfURL)

	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest("GET", libraryURL, nil)
	if err != nil {
		return fmt.Errorf("failed to create library request: %w", err)
	}
	req.Header.Set("X-Api-Key", apiKey)

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("Warning: library request failed: %v", err)
	} else {
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			var libraryBooks []map[string]interface{}
			if err := json.NewDecoder(resp.Body).Decode(&libraryBooks); err == nil {
				// Check if book title already exists in library
				for _, libBook := range libraryBooks {
					if title, ok := libBook["title"].(string); ok {
						if strings.EqualFold(title, book.Title) {
							log.Printf("Book already exists in Bookshelf library: %s", book.Title)
							return nil
						}
					}
				}
			}
		}
	}

	// Search for the book using Readarr's search API
	// Prefer ISBN for more accurate results, fallback to title + author
	searchTerm := ""
	if book.ISBN != "" {
		searchTerm = book.ISBN
		log.Printf("Searching for book by ISBN: %s", book.ISBN)
	} else {
		searchTerm = book.Title
		if book.Author != "" {
			searchTerm = fmt.Sprintf("%s %s", book.Title, book.Author)
		}
		log.Printf("Searching for book by title/author: %s", searchTerm)
	}
	searchURL := fmt.Sprintf("%s/api/v1/book/lookup?term=%s", bookshelfURL, url.QueryEscape(searchTerm))

	req, err = http.NewRequest("GET", searchURL, nil)
	if err != nil {
		return fmt.Errorf("failed to create search request: %w", err)
	}
	req.Header.Set("X-Api-Key", apiKey)

	resp, err = client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to search for book: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("search API returned status %d: %s", resp.StatusCode, string(body))
	}

	var searchResults []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&searchResults); err != nil {
		return fmt.Errorf("failed to decode search results: %w", err)
	}

	// If ISBN search failed, try with title + author
	if len(searchResults) == 0 && book.ISBN != "" {
		log.Printf("ISBN search returned no results, trying title + author...")
		fallbackTerm := book.Title
		if book.Author != "" {
			fallbackTerm = fmt.Sprintf("%s %s", book.Title, book.Author)
		}
		fallbackURL := fmt.Sprintf("%s/api/v1/book/lookup?term=%s", bookshelfURL, url.QueryEscape(fallbackTerm))
		
		req, err = http.NewRequest("GET", fallbackURL, nil)
		if err == nil {
			req.Header.Set("X-Api-Key", apiKey)
			resp, err = client.Do(req)
			if err == nil {
				defer resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					json.NewDecoder(resp.Body).Decode(&searchResults)
				}
			}
		}
	}

	if len(searchResults) == 0 {
		return fmt.Errorf("no search results found for: %s (ISBN: %s)", book.Title, book.ISBN)
	}

	// Use the first search result
	bookToAdd := searchResults[0]
	bookToAdd["monitored"] = true
	
	// Set required Readarr profile IDs on the book
	qualityProfileId := getEnvInt("QUALITY_PROFILE_ID", 1)
	metadataProfileId := getEnvInt("METADATA_PROFILE_ID", 1)
	rootFolderPath := os.Getenv("ROOT_FOLDER_PATH")
	if rootFolderPath == "" {
		rootFolderPath = "/media/books/"
	}
	
	bookToAdd["qualityProfileId"] = qualityProfileId
	bookToAdd["metadataProfileId"] = metadataProfileId
	
	// The book lookup doesn't return editions array - we need to construct it
	// from the foreignEditionId in the search result
	if bookToAdd["editions"] == nil {
		foreignEditionId := ""
		if feid, ok := bookToAdd["foreignEditionId"].(string); ok {
			foreignEditionId = feid
		}
		edition := map[string]interface{}{
			"foreignEditionId": foreignEditionId,
			"title":            bookToAdd["title"],
			"monitored":        true,
		}
		bookToAdd["editions"] = []interface{}{edition}
	}
	
	// The book lookup doesn't return a full author object, so we need to lookup the author
	// and construct a proper author object with all required fields
	authorFound := false
	if book.Author != "" {
		authorSearchURL := fmt.Sprintf("%s/api/v1/author/lookup?term=%s", bookshelfURL, url.QueryEscape(book.Author))
		authorReq, err := http.NewRequest("GET", authorSearchURL, nil)
		if err == nil {
			authorReq.Header.Set("X-Api-Key", apiKey)
			authorResp, err := client.Do(authorReq)
			if err == nil {
				defer authorResp.Body.Close()
				if authorResp.StatusCode == http.StatusOK {
					var authorResults []map[string]interface{}
					if err := json.NewDecoder(authorResp.Body).Decode(&authorResults); err == nil && len(authorResults) > 0 {
						// Find the author that best matches our target author name
						var matchedAuthor map[string]interface{}
						bookAuthorLower := strings.ToLower(book.Author)
						for _, ar := range authorResults {
							if name, ok := ar["authorName"].(string); ok {
								if strings.EqualFold(name, book.Author) {
									matchedAuthor = ar
									break
								}
								// Also check if the author name contains the target name
								if strings.Contains(strings.ToLower(name), bookAuthorLower) {
									matchedAuthor = ar
									// Don't break - keep looking for exact match
								}
							}
						}
						// Fall back to first result if no match found
						if matchedAuthor == nil {
							matchedAuthor = authorResults[0]
						}
						
						// Check if author already exists in Bookshelf (has an id)
						// If not, we can't add them because the metadata provider doesn't support
						// author lookups for all authors
						if authorId, ok := matchedAuthor["id"]; ok && authorId != nil {
							// Author already in database - safe to use
							matchedAuthor["qualityProfileId"] = qualityProfileId
							matchedAuthor["metadataProfileId"] = metadataProfileId
							matchedAuthor["monitored"] = false
							matchedAuthor["monitorNewItems"] = "none"
							matchedAuthor["rootFolderPath"] = rootFolderPath
							bookToAdd["author"] = matchedAuthor
							authorFound = true
							log.Printf("Using existing author from Bookshelf: %v (id: %v)", matchedAuthor["authorName"], authorId)
						} else {
							log.Printf("Author '%v' found in lookup but not in database (id=null), will try work endpoint", matchedAuthor["authorName"])
						}
					}
				}
			}
		}
	}
	
	// If author lookup failed but we have author data from the work endpoint, use that
	if !authorFound && workData != nil {
		if authors, ok := workData["Authors"].([]interface{}); ok && len(authors) > 0 {
			if authorData, ok := authors[0].(map[string]interface{}); ok {
				// Build author object from work data
				author := map[string]interface{}{
					"foreignAuthorId":   fmt.Sprintf("%v", authorData["ForeignId"]),
					"authorName":        authorData["Name"],
					"overview":          authorData["Description"],
					"qualityProfileId":  qualityProfileId,
					"metadataProfileId": metadataProfileId,
					"monitored":         false,
					"monitorNewItems":   "none",
					"rootFolderPath":    rootFolderPath,
				}
				bookToAdd["author"] = author
				log.Printf("Using author data from work endpoint: %v", authorData["Name"])
			}
		}
	}
	
	bookToAdd["addOptions"] = map[string]interface{}{
		"searchForNewBook": true,
	}

	bookJSON, err := json.Marshal(bookToAdd)
	if err != nil {
		return fmt.Errorf("failed to marshal book: %w", err)
	}

	addURL := fmt.Sprintf("%s/api/v1/book", bookshelfURL)
	req, err = http.NewRequest("POST", addURL, bytes.NewBuffer(bookJSON))
	if err != nil {
		return fmt.Errorf("failed to create add request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Api-Key", apiKey)

	resp, err = client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to add book: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("bookshelf API returned status %d: %s", resp.StatusCode, string(body))
	}

	log.Printf("Successfully added book to Bookshelf: %s", book.Title)
	return nil
}

func syncBooks(config *Config, db *sql.DB) error {
	log.Println("Starting book sync...")

	// Fetch want-to-read list from Hardcover (now returns full book info)
	books, err := fetchWantToReadList(config.HardcoverAPIKey)
	if err != nil {
		return err
	}

	log.Printf("Found %d books in want-to-read list", len(books))

	syncedCount := 0
	errorCount := 0
	skippedCount := 0

	for _, book := range books {
		// Check if already synced
		synced, err := isBookSynced(db, book.ID)
		if err != nil {
			log.Printf("Error checking sync status for book %d: %v", book.ID, err)
			errorCount++
			continue
		}
		if synced {
			skippedCount++
			continue
		}

		// Add directly to Bookshelf using Hardcover data
		if err := addToBookshelf(config.BookshelfURL, config.BookshelfAPIKey, config.MetadataURL, book); err != nil {
			log.Printf("Error adding book '%s' to Bookshelf: %v", book.Title, err)
			errorCount++
			continue
		}

		// Mark as synced
		if err := markBookSynced(db, book.ID, book.Title); err != nil {
			log.Printf("Error marking book %d as synced: %v", book.ID, err)
			errorCount++
			continue
		}

		syncedCount++
		log.Printf("Successfully synced book: %s by %s (ID: %d)", book.Title, book.Author, book.ID)

		// Rate limiting: wait a bit between requests
		time.Sleep(1 * time.Second)
	}

	log.Printf("Sync completed: %d new books synced, %d skipped (already synced), %d errors", syncedCount, skippedCount, errorCount)
	return nil
}

func main() {
	log.Println("Hardcover Sync starting...")

	config := loadConfig()

	// Validate configuration
	if config.HardcoverAPIKey == "" {
		log.Fatal("HARDCOVER_API_KEY is not set")
	}
	if config.BookshelfAPIKey == "" {
		log.Fatal("BOOKSHELF_API_KEY is not set")
	}

	log.Printf("Configuration loaded:")
	log.Printf("  Bookshelf URL: %s", config.BookshelfURL)
	log.Printf("  Metadata URL: %s", config.MetadataURL)
	log.Printf("  Sync Interval: %d seconds", config.SyncInterval)
	log.Printf("  Database Path: %s", config.DatabasePath)

	// Initialize database
	db, err := initDatabase(config.DatabasePath)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	log.Println("Database initialized")

	// Initial sync
	log.Println("Performing initial sync...")
	if err := syncBooks(config, db); err != nil {
		log.Printf("Error during initial sync: %v", err)
	}
	log.Println("Initial sync completed")

	// Start periodic sync
	ticker := time.NewTicker(time.Duration(config.SyncInterval) * time.Second)
	defer ticker.Stop()

	for range ticker.C {
		if err := syncBooks(config, db); err != nil {
			log.Printf("Error during sync: %v", err)
		}
	}
}
