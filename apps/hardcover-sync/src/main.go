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
	"time"

	_ "github.com/mattn/go-sqlite3"
)

type Config struct {
	HardcoverAPIKey    string
	BookshelfURL       string
	BookshelfAPIKey    string
	RReadingGlassesURL string
	SyncInterval       int
	DatabasePath       string
}

// Hardcover GraphQL types
type HardcoverResponse struct {
	Data struct {
		Me struct {
			UserBooks []struct {
				Book struct {
					ID            int    `json:"id"`
					Title         string `json:"title"`
					Contributions []struct {
						Author struct {
							Name string `json:"name"`
						} `json:"author"`
					} `json:"contributions"`
				} `json:"book"`
			} `json:"user_books"`
		} `json:"me"`
	} `json:"data"`
	Errors []struct {
		Message string `json:"message"`
	} `json:"errors"`
}

// rreading-glasses response types
type RReadingGlassesWork struct {
	ID          string   `json:"id"`
	Title       string   `json:"title"`
	Authors     []string `json:"authors"`
	ISBN        string   `json:"isbn"`
	Description string   `json:"description"`
	CoverURL    string   `json:"cover_url"`
}

// Bookshelf API types
type BookshelfBook struct {
	Title       string `json:"title"`
	Author      string `json:"author"`
	ISBN        string `json:"isbn"`
	Monitored   bool   `json:"monitored"`
	AddOptions  map[string]interface{} `json:"addOptions"`
}

// GraphQL query for fetching Want-To-Read list from Hardcover
const hardcoverWantToReadQuery = `{
	"query": "query GetWantToRead { me { user_books(where: {status_id: {_eq: 1}}) { book { id title contributions { author { name } } } } } }"
}`

func loadConfig() *Config {
	return &Config{
		HardcoverAPIKey:    getEnv("HARDCOVER_API_KEY", ""),
		BookshelfURL:       getEnv("BOOKSHELF_URL", "http://bookshelf:8787"),
		BookshelfAPIKey:    getEnv("BOOKSHELF_API_KEY", ""),
		RReadingGlassesURL: getEnv("RREADING_GLASSES_URL", "http://rreading-glasses:8080"),
		SyncInterval:       getEnvInt("SYNC_INTERVAL", 3600),
		DatabasePath:       getEnv("DATABASE_PATH", "/data/hardcover-sync.db"),
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

func fetchWantToReadList(apiKey string) ([]int, error) {
	req, err := http.NewRequest("POST", "https://api.hardcover.app/v1/graphql", bytes.NewBufferString(hardcoverWantToReadQuery))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

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

	bookIDs := make([]int, 0, len(result.Data.Me.UserBooks))
	for _, ub := range result.Data.Me.UserBooks {
		bookIDs = append(bookIDs, ub.Book.ID)
	}

	return bookIDs, nil
}

func resolveMetadata(rreadingGlassesURL string, hardcoverID int) (*RReadingGlassesWork, error) {
	url := fmt.Sprintf("%s/works/%d", rreadingGlassesURL, hardcoverID)
	
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch metadata: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("work not found in rreading-glasses")
	}

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("rreading-glasses returned status %d: %s", resp.StatusCode, string(body))
	}

	var work RReadingGlassesWork
	if err := json.NewDecoder(resp.Body).Decode(&work); err != nil {
		return nil, fmt.Errorf("failed to decode metadata: %w", err)
	}

	return &work, nil
}

func addToBookshelf(bookshelfURL, apiKey string, work *RReadingGlassesWork) error {
	// First check if book already exists
	searchTerm := work.ISBN
	if searchTerm == "" {
		searchTerm = work.Title
	}
	searchURL := fmt.Sprintf("%s/api/v1/book/lookup?term=%s", bookshelfURL, url.QueryEscape(searchTerm))

	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest("GET", searchURL, nil)
	if err != nil {
		return fmt.Errorf("failed to create lookup request: %w", err)
	}
	req.Header.Set("X-Api-Key", apiKey)

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("Warning: lookup request failed: %v", err)
	} else {
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			var existingBooks []interface{}
			if err := json.NewDecoder(resp.Body).Decode(&existingBooks); err == nil && len(existingBooks) > 0 {
				log.Printf("Book already exists in Bookshelf: %s", work.Title)
				return nil
			}
		}
	}

	// Build author string
	author := ""
	if len(work.Authors) > 0 {
		author = work.Authors[0]
	}

	// Add the book
	book := BookshelfBook{
		Title:     work.Title,
		Author:    author,
		ISBN:      work.ISBN,
		Monitored: true,
		AddOptions: map[string]interface{}{
			"searchForNewBook": true,
		},
	}

	bookJSON, err := json.Marshal(book)
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

	log.Printf("Successfully added book to Bookshelf: %s", work.Title)
	return nil
}

func syncBooks(config *Config, db *sql.DB) error {
	log.Println("Starting book sync...")

	// Fetch want-to-read list from Hardcover
	bookIDs, err := fetchWantToReadList(config.HardcoverAPIKey)
	if err != nil {
		return err
	}

	log.Printf("Found %d books in want-to-read list", len(bookIDs))

	syncedCount := 0
	errorCount := 0

	for _, hardcoverID := range bookIDs {
		// Check if already synced
		synced, err := isBookSynced(db, hardcoverID)
		if err != nil {
			log.Printf("Error checking sync status for book %d: %v", hardcoverID, err)
			errorCount++
			continue
		}
		if synced {
			log.Printf("Book %d already synced, skipping", hardcoverID)
			continue
		}

		// Resolve metadata through rreading-glasses
		work, err := resolveMetadata(config.RReadingGlassesURL, hardcoverID)
		if err != nil {
			log.Printf("Error resolving metadata for book %d: %v", hardcoverID, err)
			errorCount++
			continue
		}

		// Add to Bookshelf
		if err := addToBookshelf(config.BookshelfURL, config.BookshelfAPIKey, work); err != nil {
			log.Printf("Error adding book %d to Bookshelf: %v", hardcoverID, err)
			errorCount++
			continue
		}

		// Mark as synced
		if err := markBookSynced(db, hardcoverID, work.Title); err != nil {
			log.Printf("Error marking book %d as synced: %v", hardcoverID, err)
			errorCount++
			continue
		}

		syncedCount++
		log.Printf("Successfully synced book: %s (ID: %d)", work.Title, hardcoverID)

		// Rate limiting: wait a bit between requests
		time.Sleep(1 * time.Second)
	}

	log.Printf("Sync completed: %d new books synced, %d errors", syncedCount, errorCount)
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
	log.Printf("  rreading-glasses URL: %s", config.RReadingGlassesURL)
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
