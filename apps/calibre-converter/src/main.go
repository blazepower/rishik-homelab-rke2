package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/fsnotify/fsnotify"
	_ "github.com/mattn/go-sqlite3"
)

// ConversionTimeout is the maximum time allowed for a single conversion
const ConversionTimeout = 30 * time.Minute

// MaxStabilityWait is the maximum time to wait for file stability
const MaxStabilityWait = 5 * time.Minute

// Config holds all configuration values
type Config struct {
	WatchPath       string
	ScanInterval    int
	MaxConcurrent   int
	DatabasePath    string
	InputExtensions []string
	OutputFormat    string
	MinOutputSize   int64
	StabilityWait   int
}

// processingLock prevents multiple workers from processing the same file
var (
	processingLock sync.Mutex
	processingMap  = make(map[string]bool)
)

// tryAcquireFileLock attempts to acquire a lock for processing a file
func tryAcquireFileLock(path string) bool {
	processingLock.Lock()
	defer processingLock.Unlock()
	if processingMap[path] {
		return false
	}
	processingMap[path] = true
	return true
}

// releaseFileLock releases the processing lock for a file
func releaseFileLock(path string) {
	processingLock.Lock()
	defer processingLock.Unlock()
	delete(processingMap, path)
}

func loadConfig() *Config {
	return &Config{
		WatchPath:       getEnv("WATCH_PATH", "/media/books"),
		ScanInterval:    getEnvInt("SCAN_INTERVAL", 300),
		MaxConcurrent:   getEnvInt("MAX_CONCURRENT", 2),
		DatabasePath:    getEnv("DATABASE_PATH", "/data/calibre-converter.db"),
		InputExtensions: strings.Split(getEnv("INPUT_EXTENSIONS", ".pdf,.mobi,.azw3,.azw,.djvu,.docx,.rtf,.txt,.html,.htm,.cbz,.cbr"), ","),
		OutputFormat:    getEnv("OUTPUT_FORMAT", "epub"),
		MinOutputSize:   getEnvInt64("MIN_OUTPUT_SIZE", 10240),
		StabilityWait:   getEnvInt("STABILITY_WAIT", 5),
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

func getEnvInt64(key string, defaultValue int64) int64 {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	int64Value, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return defaultValue
	}
	return int64Value
}

func initDatabase(dbPath string) (*sql.DB, error) {
	// Ensure directory exists
	dir := filepath.Dir(dbPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create database directory: %w", err)
	}

	// Enable WAL mode for better concurrency
	dsn := dbPath + "?_journal_mode=WAL"

	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("failed to open database: %w", err)
	}

	// Set connection pool limits for concurrency
	db.SetMaxOpenConns(5)
	db.SetMaxIdleConns(5)

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS converted_files (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		input_path TEXT UNIQUE NOT NULL,
		output_path TEXT NOT NULL,
		input_size INTEGER NOT NULL,
		output_size INTEGER NOT NULL,
		converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		duration_ms INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_input_path ON converted_files(input_path);
	`

	if _, err := db.Exec(createTableSQL); err != nil {
		return nil, fmt.Errorf("failed to create table: %w", err)
	}

	return db, nil
}

func isFileConverted(db *sql.DB, inputPath string) (bool, error) {
	var count int
	err := db.QueryRow("SELECT COUNT(*) FROM converted_files WHERE input_path = ?", inputPath).Scan(&count)
	if err != nil {
		return false, err
	}
	return count > 0, nil
}

func markFileConverted(db *sql.DB, inputPath, outputPath string, inputSize, outputSize int64, durationMs int64) error {
	_, err := db.Exec(
		"INSERT OR IGNORE INTO converted_files (input_path, output_path, input_size, output_size, duration_ms) VALUES (?, ?, ?, ?, ?)",
		inputPath, outputPath, inputSize, outputSize, durationMs,
	)
	return err
}

func isSupportedInputFile(filename string, extensions []string) bool {
	lowerFilename := strings.ToLower(filename)
	for _, ext := range extensions {
		if strings.HasSuffix(lowerFilename, strings.TrimSpace(ext)) {
			return true
		}
	}
	return false
}

func isEpubFile(filename string) bool {
	return strings.HasSuffix(strings.ToLower(filename), ".epub")
}

func getOutputPath(inputPath, outputFormat string) string {
	dir := filepath.Dir(inputPath)
	base := filepath.Base(inputPath)
	ext := filepath.Ext(base)
	name := strings.TrimSuffix(base, ext)
	if name == "" {
		// For files like ".hidden", use the original base name
		name = base
	}
	return filepath.Join(dir, name+"."+outputFormat)
}

// generateTempPath creates a unique temp file path with random suffix
func generateTempPath(outputPath string) (string, error) {
	randBytes := make([]byte, 8)
	if _, err := rand.Read(randBytes); err != nil {
		return "", fmt.Errorf("failed to generate random bytes: %w", err)
	}
	return outputPath + "." + hex.EncodeToString(randBytes) + ".tmp", nil
}

// waitForFileStability waits for a file's size to remain stable
// Returns false if file is empty, deleted, or doesn't stabilize within MaxStabilityWait
func waitForFileStability(filePath string, stabilityWait int) bool {
	checkInterval := time.Second
	stableCount := 0
	requiredStable := stabilityWait // Number of seconds file must be stable
	var lastSize int64 = -1
	startTime := time.Now()

	for stableCount < requiredStable {
		// Check for maximum wait time to prevent infinite loop on empty files
		if time.Since(startTime) > MaxStabilityWait {
			log.Printf("File %s: stability wait exceeded maximum time of %v", filepath.Base(filePath), MaxStabilityWait)
			return false
		}

		fileInfo, err := os.Stat(filePath)
		if err != nil {
			// File may not exist yet or was deleted
			return false
		}

		currentSize := fileInfo.Size()

		// Reject empty files immediately after initial check
		if currentSize == 0 && lastSize == 0 {
			log.Printf("File %s: rejecting empty file (0 bytes)", filepath.Base(filePath))
			return false
		}

		if currentSize == lastSize && currentSize > 0 {
			stableCount++
		} else {
			stableCount = 0
		}

		lastSize = currentSize
		time.Sleep(checkInterval)
	}

	return true
}

// removeTempFile removes a temp file and logs a warning if it fails
func removeTempFile(tempPath string) {
	if err := os.Remove(tempPath); err != nil && !os.IsNotExist(err) {
		log.Printf("Warning: failed to remove temp file %s: %v", tempPath, err)
	}
}

// validateInputPath checks that the input path is within expected boundaries
func validateInputPath(inputPath, watchPath string) error {
	absInput, err := filepath.Abs(inputPath)
	if err != nil {
		return fmt.Errorf("failed to get absolute path: %w", err)
	}
	absWatch, err := filepath.Abs(watchPath)
	if err != nil {
		return fmt.Errorf("failed to get watch path absolute path: %w", err)
	}
	if !strings.HasPrefix(absInput, absWatch) {
		return fmt.Errorf("input path %s is not within watch path %s", absInput, absWatch)
	}
	return nil
}

func convertFile(inputPath string, config *Config) (string, int64, error) {
	outputPath := getOutputPath(inputPath, config.OutputFormat)

	// Create unique temp file for atomic write to prevent collisions
	tempPath, err := generateTempPath(outputPath)
	if err != nil {
		return "", 0, fmt.Errorf("failed to generate temp path: %w", err)
	}

	// Run ebook-convert with timeout to prevent hanging on problematic files
	ctx, cancel := context.WithTimeout(context.Background(), ConversionTimeout)
	defer cancel()

	startTime := time.Now()
	cmd := exec.CommandContext(ctx, "ebook-convert", inputPath, tempPath)
	output, err := cmd.CombinedOutput()
	duration := time.Since(startTime)

	if err != nil {
		// Clean up temp file on error
		removeTempFile(tempPath)
		if ctx.Err() == context.DeadlineExceeded {
			return "", 0, fmt.Errorf("ebook-convert timed out after %v", ConversionTimeout)
		}
		return "", 0, fmt.Errorf("ebook-convert failed: %w, output: %s", err, string(output))
	}

	// Check temp file exists and has sufficient size
	tempInfo, err := os.Stat(tempPath)
	if err != nil {
		removeTempFile(tempPath)
		return "", 0, fmt.Errorf("failed to stat temp output file: %w", err)
	}

	if tempInfo.Size() < config.MinOutputSize {
		removeTempFile(tempPath)
		return "", 0, fmt.Errorf("output file too small: %d bytes (min: %d)", tempInfo.Size(), config.MinOutputSize)
	}

	// Move temp file to final location (atomic on same filesystem)
	if err := os.Rename(tempPath, outputPath); err != nil {
		removeTempFile(tempPath)
		return "", 0, fmt.Errorf("failed to rename temp file: %w", err)
	}

	log.Printf("Converted %s -> %s (size: %d bytes, duration: %v)",
		filepath.Base(inputPath), filepath.Base(outputPath), tempInfo.Size(), duration)

	return outputPath, duration.Milliseconds(), nil
}

func processFile(inputPath string, config *Config, db *sql.DB) error {
	// Skip if it's an epub file (output format)
	if isEpubFile(inputPath) {
		return nil
	}

	// Try to acquire processing lock to prevent race conditions
	if !tryAcquireFileLock(inputPath) {
		log.Printf("Skipping %s: already being processed by another worker", filepath.Base(inputPath))
		return nil
	}
	defer releaseFileLock(inputPath)

	// Check if already converted
	converted, err := isFileConverted(db, inputPath)
	if err != nil {
		return fmt.Errorf("failed to check if file converted: %w", err)
	}
	if converted {
		log.Printf("Skipping %s: already converted", filepath.Base(inputPath))
		return nil
	}

	// Get input file info
	inputInfo, err := os.Stat(inputPath)
	if err != nil {
		return fmt.Errorf("failed to stat input file: %w", err)
	}

	// Check if epub with same base name already exists (idempotency)
	outputPath := getOutputPath(inputPath, config.OutputFormat)
	if outputInfo, err := os.Stat(outputPath); err == nil {
		log.Printf("Skipping %s: output file %s already exists", filepath.Base(inputPath), filepath.Base(outputPath))
		// Mark as converted so we don't check again, using actual output size and sentinel duration (-1)
		if err := markFileConverted(db, inputPath, outputPath, inputInfo.Size(), outputInfo.Size(), -1); err != nil {
			log.Printf("Warning: failed to mark file as converted: %v", err)
		}
		return nil
	}

	log.Printf("Converting %s...", filepath.Base(inputPath))

	// Perform conversion
	finalOutputPath, durationMs, err := convertFile(inputPath, config)
	if err != nil {
		return fmt.Errorf("conversion failed: %w", err)
	}

	// Verify output exists and has sufficient size
	outputInfo, err := os.Stat(finalOutputPath)
	if err != nil {
		return fmt.Errorf("failed to verify output file: %w", err)
	}

	if outputInfo.Size() < config.MinOutputSize {
		return fmt.Errorf("output file verification failed: size %d < min %d", outputInfo.Size(), config.MinOutputSize)
	}

	// Mark as converted in database
	if err := markFileConverted(db, inputPath, finalOutputPath, inputInfo.Size(), outputInfo.Size(), durationMs); err != nil {
		return fmt.Errorf("failed to mark file as converted: %w", err)
	}

	// Delete original file only after successful conversion
	if err := os.Remove(inputPath); err != nil {
		log.Printf("Warning: failed to delete original file %s: %v", filepath.Base(inputPath), err)
	} else {
		log.Printf("Deleted original file: %s", filepath.Base(inputPath))
	}

	log.Printf("Successfully converted %s -> %s", filepath.Base(inputPath), filepath.Base(finalOutputPath))
	return nil
}

func scanDirectory(watchPath string, config *Config, db *sql.DB, jobChan chan<- string) error {
	return filepath.Walk(watchPath, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			log.Printf("Error accessing path %s: %v", path, err)
			return nil // Continue walking
		}

		if info.IsDir() {
			return nil
		}

		// Skip epub files
		if isEpubFile(info.Name()) {
			return nil
		}

		if !isSupportedInputFile(info.Name(), config.InputExtensions) {
			return nil
		}

		// Check if already converted
		converted, err := isFileConverted(db, path)
		if err != nil {
			log.Printf("Error checking conversion status for %s: %v", path, err)
			return nil
		}
		if converted {
			return nil
		}

		// Check if output already exists
		outputPath := getOutputPath(path, config.OutputFormat)
		if _, err := os.Stat(outputPath); err == nil {
			return nil
		}

		// Queue for conversion
		select {
		case jobChan <- path:
		default:
			log.Printf("Job queue full, skipping %s for now (will be picked up by periodic scan)", filepath.Base(path))
		}

		return nil
	})
}

func worker(id int, config *Config, db *sql.DB, jobs <-chan string, wg *sync.WaitGroup) {
	defer wg.Done()
	for inputPath := range jobs {
		log.Printf("Worker %d: processing %s", id, filepath.Base(inputPath))
		if err := processFile(inputPath, config, db); err != nil {
			log.Printf("Worker %d: error processing %s: %v", id, filepath.Base(inputPath), err)
		}
	}
}

func watchDirectory(ctx context.Context, watchPath string, config *Config, db *sql.DB, jobChan chan<- string) error {
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
		case <-ctx.Done():
			log.Println("Watcher shutting down...")
			return nil
		case event, ok := <-watcher.Events:
			if !ok {
				return nil
			}

			if event.Op&fsnotify.Create == fsnotify.Create {
				fileInfo, err := os.Stat(event.Name)
				if err != nil {
					continue
				}

				if fileInfo.IsDir() {
					// Add new directory to watcher with error checking
					if err := watcher.Add(event.Name); err != nil {
						log.Printf("Failed to add new directory to watcher: %s: %v", event.Name, err)
					}
					continue
				}

				// Skip epub files (our output format)
				if isEpubFile(event.Name) {
					continue
				}

				// Check if it's a supported input file
				if !isSupportedInputFile(event.Name, config.InputExtensions) {
					continue
				}

				// Wait for file to be fully written
				log.Printf("Detected new file: %s, waiting for write to complete...", filepath.Base(event.Name))
				if !waitForFileStability(event.Name, config.StabilityWait) {
					log.Printf("File %s not stable or deleted, skipping", filepath.Base(event.Name))
					continue
				}

				// Queue for conversion with context awareness
				select {
				case <-ctx.Done():
					log.Printf("Context cancelled while waiting to queue %s", filepath.Base(event.Name))
					return nil
				case jobChan <- event.Name:
					log.Printf("Queued %s for conversion", filepath.Base(event.Name))
				default:
					log.Printf("Job queue full, skipping %s (will be picked up by periodic scan)", filepath.Base(event.Name))
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
	log.Println("Calibre Converter starting...")

	config := loadConfig()

	log.Printf("Configuration loaded:")
	log.Printf("  Watch Path: %s", config.WatchPath)
	log.Printf("  Scan Interval: %d seconds", config.ScanInterval)
	log.Printf("  Max Concurrent: %d", config.MaxConcurrent)
	log.Printf("  Database Path: %s", config.DatabasePath)
	log.Printf("  Input Extensions: %v", config.InputExtensions)
	log.Printf("  Output Format: %s", config.OutputFormat)
	log.Printf("  Min Output Size: %d bytes", config.MinOutputSize)
	log.Printf("  Stability Wait: %d seconds", config.StabilityWait)

	// Verify ebook-convert is available
	if _, err := exec.LookPath("ebook-convert"); err != nil {
		log.Fatalf("ebook-convert not found in PATH: %v", err)
	}
	log.Println("ebook-convert binary found")

	// Initialize database
	db, err := initDatabase(config.DatabasePath)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	log.Println("Database initialized")

	// Create job channel and start workers
	jobChan := make(chan string, 100)
	var wg sync.WaitGroup

	for i := 1; i <= config.MaxConcurrent; i++ {
		wg.Add(1)
		go worker(i, config, db, jobChan, &wg)
	}
	log.Printf("Started %d conversion workers", config.MaxConcurrent)

	// Initial scan
	log.Println("Performing initial scan...")
	if err := scanDirectory(config.WatchPath, config, db, jobChan); err != nil {
		log.Printf("Error during initial scan: %v", err)
	}
	log.Println("Initial scan completed")

	// Start periodic scanning
	ticker := time.NewTicker(time.Duration(config.ScanInterval) * time.Second)

	// Setup signal handling for graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	// Channel to signal periodic scanner has stopped
	scannerDone := make(chan struct{})

	go func() {
		sig := <-sigChan
		log.Printf("Received signal %v, initiating shutdown...", sig)
		cancel()
	}()

	go func() {
		defer close(scannerDone)
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return
			case <-ticker.C:
				log.Println("Performing periodic scan...")
				if err := scanDirectory(config.WatchPath, config, db, jobChan); err != nil {
					log.Printf("Error during periodic scan: %v", err)
				}
			}
		}
	}()

	// Start watching for new files
	log.Println("Starting file watcher...")
	if err := watchDirectory(ctx, config.WatchPath, config, db, jobChan); err != nil {
		log.Printf("Error watching directory: %v", err)
	}

	// Wait for periodic scanner to stop before closing jobChan
	log.Println("Waiting for periodic scanner to stop...")
	<-scannerDone

	// Clean shutdown - close jobChan only after all writers have stopped
	log.Println("Shutting down workers...")
	close(jobChan)
	wg.Wait()
	log.Println("Calibre Converter shutdown complete")
}
