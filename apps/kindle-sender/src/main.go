package main

import (
	"database/sql"
	"encoding/base64"
	"fmt"
	"log"
	"net/smtp"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/fsnotify/fsnotify"
	_ "github.com/mattn/go-sqlite3"
)

type Config struct {
	WatchPath       string
	ScanInterval    int
	MaxFileSizeMB   int
	FileExtensions  []string
	SMTPHost        string
	SMTPPort        string
	SMTPUser        string
	SMTPPassword    string
	KindleEmail     string
	SenderEmail     string
	DatabasePath    string
}

type EmailMessage struct {
	From        string
	To          string
	Subject     string
	Body        string
	Attachment  string
	ContentType string
}

func loadConfig() *Config {
	return &Config{
		WatchPath:      getEnv("WATCH_PATH", "/media/books"),
		ScanInterval:   getEnvInt("SCAN_INTERVAL", 300),
		MaxFileSizeMB:  getEnvInt("MAX_FILE_SIZE_MB", 50),
		FileExtensions: strings.Split(getEnv("FILE_EXTENSIONS", ".epub,.mobi,.azw3,.pdf"), ","),
		SMTPHost:       getEnv("SMTP_HOST", ""),
		SMTPPort:       getEnv("SMTP_PORT", "587"),
		SMTPUser:       getEnv("SMTP_USER", ""),
		SMTPPassword:   getEnv("SMTP_PASSWORD", ""),
		KindleEmail:    getEnv("KINDLE_EMAIL", ""),
		SenderEmail:    getEnv("SENDER_EMAIL", getEnv("SMTP_USER", "")),
		DatabasePath:   getEnv("DATABASE_PATH", "/data/kindle-sender.db"),
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
	CREATE TABLE IF NOT EXISTS sent_files (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		file_path TEXT UNIQUE NOT NULL,
		file_size INTEGER NOT NULL,
		sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		email_sent BOOLEAN DEFAULT 1
	);
	CREATE INDEX IF NOT EXISTS idx_file_path ON sent_files(file_path);
	`

	if _, err := db.Exec(createTableSQL); err != nil {
		return nil, fmt.Errorf("failed to create table: %w", err)
	}

	return db, nil
}

func isFileSent(db *sql.DB, filePath string) (bool, error) {
	var count int
	err := db.QueryRow("SELECT COUNT(*) FROM sent_files WHERE file_path = ?", filePath).Scan(&count)
	if err != nil {
		return false, err
	}
	return count > 0, nil
}

func markFileSent(db *sql.DB, filePath string, fileSize int64) error {
	_, err := db.Exec(
		"INSERT OR IGNORE INTO sent_files (file_path, file_size) VALUES (?, ?)",
		filePath, fileSize,
	)
	return err
}

func isSupportedFile(filename string, extensions []string) bool {
	lowerFilename := strings.ToLower(filename)
	for _, ext := range extensions {
		if strings.HasSuffix(lowerFilename, strings.TrimSpace(ext)) {
			return true
		}
	}
	return false
}

func sendEmail(msg *EmailMessage, config *Config) error {
	// Read attachment
	fileData, err := os.ReadFile(msg.Attachment)
	if err != nil {
		return fmt.Errorf("failed to read attachment: %w", err)
	}

	// Build email
	boundary := "kindle-sender-boundary"
	headers := make(map[string]string)
	headers["From"] = msg.From
	headers["To"] = msg.To
	headers["Subject"] = msg.Subject
	headers["MIME-Version"] = "1.0"
	headers["Content-Type"] = fmt.Sprintf("multipart/mixed; boundary=%s", boundary)

	var emailBody strings.Builder
	for k, v := range headers {
		emailBody.WriteString(fmt.Sprintf("%s: %s\r\n", k, v))
	}
	emailBody.WriteString("\r\n")

	// Body part
	emailBody.WriteString(fmt.Sprintf("--%s\r\n", boundary))
	emailBody.WriteString("Content-Type: text/plain; charset=utf-8\r\n\r\n")
	emailBody.WriteString(msg.Body)
	emailBody.WriteString("\r\n\r\n")

	// Attachment part
	emailBody.WriteString(fmt.Sprintf("--%s\r\n", boundary))
	emailBody.WriteString(fmt.Sprintf("Content-Type: %s\r\n", msg.ContentType))
	emailBody.WriteString("Content-Transfer-Encoding: base64\r\n")
	emailBody.WriteString(fmt.Sprintf("Content-Disposition: attachment; filename=\"%s\"\r\n\r\n",
		filepath.Base(msg.Attachment)))

	// Encode attachment to base64 using standard library
	encoded := base64.StdEncoding.EncodeToString(fileData)
	// Add line breaks every 76 characters per RFC 2045
	for i := 0; i < len(encoded); i += 76 {
		end := i + 76
		if end > len(encoded) {
			end = len(encoded)
		}
		emailBody.WriteString(encoded[i:end])
		emailBody.WriteString("\r\n")
	}
	
	emailBody.WriteString(fmt.Sprintf("--%s--", boundary))

	// Send via SMTP
	auth := smtp.PlainAuth("", config.SMTPUser, config.SMTPPassword, config.SMTPHost)
	addr := fmt.Sprintf("%s:%s", config.SMTPHost, config.SMTPPort)

	err = smtp.SendMail(
		addr,
		auth,
		msg.From,
		[]string{msg.To},
		[]byte(emailBody.String()),
	)

	return err
}

func getContentType(filename string) string {
	ext := strings.ToLower(filepath.Ext(filename))
	switch ext {
	case ".epub":
		return "application/epub+zip"
	case ".mobi":
		return "application/x-mobipocket-ebook"
	case ".azw3":
		return "application/vnd.amazon.ebook"
	case ".pdf":
		return "application/pdf"
	default:
		return "application/octet-stream"
	}
}

func processFile(filePath string, config *Config, db *sql.DB) error {
	// Check file size
	fileInfo, err := os.Stat(filePath)
	if err != nil {
		return fmt.Errorf("failed to stat file: %w", err)
	}

	maxSize := int64(config.MaxFileSizeMB) * 1024 * 1024
	if fileInfo.Size() > maxSize {
		log.Printf("Skipping %s: file size %d exceeds max %d bytes",
			filePath, fileInfo.Size(), maxSize)
		return nil
	}

	// Check if already sent
	sent, err := isFileSent(db, filePath)
	if err != nil {
		return fmt.Errorf("failed to check if file sent: %w", err)
	}
	if sent {
		log.Printf("Skipping %s: already sent", filePath)
		return nil
	}

	// Send email
	msg := &EmailMessage{
		From:        config.SenderEmail,
		To:          config.KindleEmail,
		Subject:     fmt.Sprintf("Book: %s", filepath.Base(filePath)),
		Body:        fmt.Sprintf("Automatically sent by Kindle Sender\n\nFile: %s\nSize: %d bytes", filepath.Base(filePath), fileInfo.Size()),
		Attachment:  filePath,
		ContentType: getContentType(filePath),
	}

	log.Printf("Sending %s to %s...", filepath.Base(filePath), config.KindleEmail)
	if err := sendEmail(msg, config); err != nil {
		return fmt.Errorf("failed to send email: %w", err)
	}

	// Mark as sent
	if err := markFileSent(db, filePath, fileInfo.Size()); err != nil {
		return fmt.Errorf("failed to mark file as sent: %w", err)
	}

	log.Printf("Successfully sent %s", filepath.Base(filePath))
	return nil
}

func scanDirectory(watchPath string, config *Config, db *sql.DB) error {
	return filepath.Walk(watchPath, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			log.Printf("Error accessing path %s: %v", path, err)
			return nil // Continue walking
		}

		if info.IsDir() {
			return nil
		}

		if !isSupportedFile(info.Name(), config.FileExtensions) {
			return nil
		}

		if err := processFile(path, config, db); err != nil {
			log.Printf("Error processing file %s: %v", path, err)
		}

		return nil
	})
}

func watchDirectory(watchPath string, config *Config, db *sql.DB) error {
	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("failed to create watcher: %w", err)
	}
	defer watcher.Close()

	// Watch the directory recursively
	if err := filepath.Walk(watchPath, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return watcher.Add(path)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("failed to add watch paths: %w", err)
	}

	log.Printf("Watching directory: %s", watchPath)

	// Process events
	for {
		select {
		case event, ok := <-watcher.Events:
			if !ok {
				return nil
			}

			if event.Op&fsnotify.Create == fsnotify.Create {
				// Wait a bit for file to be fully written
				time.Sleep(2 * time.Second)

				fileInfo, err := os.Stat(event.Name)
				if err != nil {
					log.Printf("Error stating file %s: %v", event.Name, err)
					continue
				}

				if fileInfo.IsDir() {
					// Add new directory to watcher
					watcher.Add(event.Name)
					continue
				}

				if isSupportedFile(event.Name, config.FileExtensions) {
					if err := processFile(event.Name, config, db); err != nil {
						log.Printf("Error processing file %s: %v", event.Name, err)
					}
				}
			}

		case err, ok := <-watcher.Errors:
			if !ok {
				return nil
			}
			log.Printf("Watcher error: %v", err)
		}
	}
}

func main() {
	log.Println("Kindle Sender starting...")

	config := loadConfig()

	// Validate configuration
	if config.SMTPHost == "" || config.SMTPUser == "" || config.SMTPPassword == "" {
		log.Fatal("SMTP configuration is incomplete. Please set SMTP_HOST, SMTP_USER, and SMTP_PASSWORD")
	}
	if config.KindleEmail == "" {
		log.Fatal("KINDLE_EMAIL is not set")
	}

	log.Printf("Configuration loaded:")
	log.Printf("  Watch Path: %s", config.WatchPath)
	log.Printf("  Scan Interval: %d seconds", config.ScanInterval)
	log.Printf("  Max File Size: %d MB", config.MaxFileSizeMB)
	log.Printf("  File Extensions: %v", config.FileExtensions)
	log.Printf("  SMTP Host: %s:%s", config.SMTPHost, config.SMTPPort)
	log.Printf("  Kindle Email: %s", config.KindleEmail)

	// Initialize database
	db, err := initDatabase(config.DatabasePath)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	log.Println("Database initialized")

	// Initial scan
	log.Println("Performing initial scan...")
	if err := scanDirectory(config.WatchPath, config, db); err != nil {
		log.Printf("Error during initial scan: %v", err)
	}
	log.Println("Initial scan completed")

	// Start periodic scanning
	ticker := time.NewTicker(time.Duration(config.ScanInterval) * time.Second)
	defer ticker.Stop()

	go func() {
		for range ticker.C {
			log.Println("Performing periodic scan...")
			if err := scanDirectory(config.WatchPath, config, db); err != nil {
				log.Printf("Error during periodic scan: %v", err)
			}
		}
	}()

	// Start watching for new files
	log.Println("Starting file watcher...")
	if err := watchDirectory(config.WatchPath, config, db); err != nil {
		log.Fatalf("Error watching directory: %v", err)
	}
}
