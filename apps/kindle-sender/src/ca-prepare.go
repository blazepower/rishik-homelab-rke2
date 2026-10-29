//go:build ignore

// Build-only tool: remove exact public-key matches before regenerating trust paths.
package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"os"
	"path/filepath"
)

func main() {
	if len(os.Args) != 3 {
		panic("usage: kindle-clear-ca certificate-directory policy.json")
	}
	var policy struct {
		Blocked []struct {
			Name string `json:"name"`
			Hash string `json:"sha256"`
		} `json:"blocked_spkis"`
	}
	data, err := os.ReadFile(os.Args[2])
	must(err)
	must(json.Unmarshal(data, &policy))
	if len(policy.Blocked) != 11 {
		panic("expected eleven canonical SPKIs")
	}
	denied := make(map[[32]byte]bool)
	for _, entry := range policy.Blocked {
		b, err := hex.DecodeString(entry.Hash)
		must(err)
		if len(b) != 32 || entry.Name == "" {
			panic("invalid policy entry")
		}
		var hash [32]byte
		copy(hash[:], b)
		if denied[hash] {
			panic("duplicate policy entry")
		}
		denied[hash] = true
	}
	count := 0
	must(filepath.WalkDir(os.Args[1], func(path string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() || filepath.Ext(path) != ".crt" {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		certs := 0
		remove := false
		for len(bytes.TrimSpace(data)) > 0 {
			block, rest := pem.Decode(data)
			if block == nil {
				panic("unparseable certificate file: " + path)
			}
			data = rest
			if block.Type != "CERTIFICATE" {
				panic("unexpected PEM object: " + path)
			}
			cert, err := x509.ParseCertificate(block.Bytes)
			must(err)
			certs++
			hash := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
			if denied[hash] {
				remove = true
				fmt.Printf("removed public SPKI %x from %s\n", hash, path)
			}
		}
		if certs != 1 {
			panic("expected one certificate per public source file: " + path)
		}
		count += certs
		if remove {
			return os.Remove(path)
		}
		return nil
	}))
	if count < 10 {
		panic("unexpectedly small input trust store")
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
