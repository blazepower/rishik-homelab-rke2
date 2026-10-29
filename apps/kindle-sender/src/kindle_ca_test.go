package main

import (
	"bytes"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Public SPKIs only, extracted from the incident's fingerprint-identified certs.
//
//go:embed kindle-ca-spkis.json
var kindleSPKIs []byte

type kindlePublicKey struct {
	Hash string `json:"spki_sha256"`
	DER  string `json:"spki_der_base64"`
}

func kindlePolicy(t *testing.T) map[string]bool {
	t.Helper()
	path := os.Getenv("KINDLE_CA_POLICY")
	if path == "" {
		path = filepath.Join("..", "..", "..", ".github", "security", "emudhra-2026.json")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var policy struct {
		Blocked []struct {
			Hash string `json:"sha256"`
		} `json:"blocked_spkis"`
	}
	if err := json.Unmarshal(data, &policy); err != nil {
		t.Fatal(err)
	}
	denied := map[string]bool{}
	for _, entry := range policy.Blocked {
		hash := strings.ToLower(entry.Hash)
		b, err := hex.DecodeString(hash)
		if err != nil || len(b) != 32 || denied[hash] {
			t.Fatal("invalid or duplicate canonical SPKI")
		}
		denied[hash] = true
	}
	if len(denied) != 11 {
		t.Fatal("expected eleven canonical SPKIs")
	}
	return denied
}

func kindleKeys(t *testing.T) []kindlePublicKey {
	t.Helper()
	var keys []kindlePublicKey
	if err := json.Unmarshal(kindleSPKIs, &keys); err != nil {
		t.Fatal(err)
	}
	if len(keys) != 8 {
		t.Fatal("expected all eight compromised intermediate public keys")
	}
	denied, seen := kindlePolicy(t), map[string]bool{}
	for _, key := range keys {
		b, err := base64.StdEncoding.DecodeString(key.DER)
		hash := sha256.Sum256(b)
		if err != nil || hex.EncodeToString(hash[:]) != key.Hash || !denied[key.Hash] || seen[key.Hash] {
			t.Fatal("public fixture does not match canonical SPKI")
		}
		seen[key.Hash] = true
	}
	return keys
}

func kindleCertificate(t *testing.T, pub crypto.PublicKey) (*x509.Certificate, *ecdsa.PrivateKey, *x509.CertPool) {
	t.Helper()
	rootKey, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	root := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Isolated Kindle CA test"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	rootDER, err := x509.CreateCertificate(rand.Reader, root, root, &rootKey.PublicKey, rootKey)
	if err != nil {
		t.Fatal(err)
	}
	root, err = x509.ParseCertificate(rootDER)
	if err != nil {
		t.Fatal(err)
	}
	leafKey, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if pub == nil {
		pub = &leafKey.PublicKey
	}
	leaf := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "kindle-ca.invalid"},
		DNSNames: []string{"kindle-ca.invalid"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, leaf, root, pub, rootKey)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err = x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	if err := leaf.CheckSignatureFrom(root); err != nil {
		t.Fatal("negative control must have a valid certificate signature:", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(root)
	return leaf, leafKey, pool
}

func TestKindleCAToolchain(t *testing.T) {
	if runtime.Version() != "go1.27.3" {
		t.Fatalf("expected pinned native Go 1.27.3; got %s", runtime.Version())
	}
	t.Log("native toolchain:", runtime.Version())
}

func TestKindleCANativeDenylist(t *testing.T) {
	for _, key := range kindleKeys(t) {
		t.Run(key.Hash, func(t *testing.T) {
			der, _ := base64.StdEncoding.DecodeString(key.DER)
			pub, err := x509.ParsePKIXPublicKey(der)
			if err != nil {
				t.Fatal(err)
			}
			leaf, _, roots := kindleCertificate(t, pub)
			_, err = leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: "kindle-ca.invalid"})
			if err == nil || err.Error() != "x509: no valid chains built" {
				t.Fatalf("valid synthetic chain containing exact denied SPKI was not natively rejected: %v", err)
			}
		})
	}
}

func kindleHandshake(cert *x509.Certificate, key *ecdsa.PrivateKey, roots *x509.CertPool) error {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()
	done := make(chan error, 1)
	go func() {
		wire, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer wire.Close()
		wire.SetDeadline(time.Now().Add(5 * time.Second))
		server := tls.Server(wire, &tls.Config{
			Certificates: []tls.Certificate{{Certificate: [][]byte{cert.Raw}, PrivateKey: key}},
			MinVersion:   tls.VersionTLS13, SessionTicketsDisabled: true,
		})
		done <- server.Handshake()
	}()
	client, err := tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", listener.Addr().String(), &tls.Config{
		RootCAs: roots, ServerName: "kindle-ca.invalid", MinVersion: tls.VersionTLS13,
	})
	if client != nil {
		client.Close()
	}
	listener.Close()
	<-done
	return err
}

func TestKindleCATLSControls(t *testing.T) {
	cert, key, roots := kindleCertificate(t, nil)
	if _, err := cert.Verify(x509.VerifyOptions{Roots: roots, DNSName: "kindle-ca.invalid"}); err != nil {
		t.Fatal("native valid-chain positive control failed:", err)
	}
	if err := kindleHandshake(cert, key, roots); err != nil {
		t.Fatal("isolated valid TLS positive control failed:", err)
	}
	var unknown x509.UnknownAuthorityError
	if err := kindleHandshake(cert, key, x509.NewCertPool()); !errors.As(err, &unknown) {
		t.Fatalf("ordinary unknown-root TLS negative control failed: %v", err)
	}
	for _, fixture := range kindleKeys(t) {
		t.Run(fixture.Hash, func(t *testing.T) {
			der, _ := base64.StdEncoding.DecodeString(fixture.DER)
			pub, err := x509.ParsePKIXPublicKey(der)
			if err != nil {
				t.Fatal(err)
			}
			cert, key, roots := kindleCertificate(t, pub)
			// No compromised private key is used. Require chain-policy rejection,
			// not the later CertificateVerify signature failure from this test key.
			err = kindleHandshake(cert, key, roots)
			var verification *tls.CertificateVerificationError
			if !errors.As(err, &verification) || verification.Err.Error() != "x509: no valid chains built" {
				t.Fatalf("TLS did not reject exact SPKI during certificate validation: %v", err)
			}
		})
	}
}

func TestKindleCARuntimeRoots(t *testing.T) {
	if os.Getenv("KINDLE_CA_RUNTIME") != "1" {
		t.Skip("runtime root evidence is required by the isolated Docker acceptance target")
	}
	denied := kindlePolicy(t)
	for _, directory := range []string{"/etc/ssl/certs", "/usr/share/ca-certificates"} {
		count := 0
		err := filepath.WalkDir(directory, func(path string, entry os.DirEntry, err error) error {
			if err != nil || entry.IsDir() {
				return err
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			inFile := 0
			for len(bytes.TrimSpace(data)) > 0 {
				block, rest := pem.Decode(data)
				if block == nil {
					t.Fatalf("malformed certificate data in runtime trust path %s", path)
				}
				data = rest
				if block.Type != "CERTIFICATE" {
					t.Fatalf("unexpected PEM object in runtime trust path %s", path)
				}
				cert, err := x509.ParseCertificate(block.Bytes)
				if err != nil {
					return err
				}
				count++
				inFile++
				hash := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
				if denied[hex.EncodeToString(hash[:])] {
					t.Fatalf("runtime trust path %s retains denied public SPKI %x", path, hash)
				}
				if inFile == 0 {
					t.Fatalf("empty certificate file in runtime trust path %s", path)
				}
			}
			return nil
		})
		if err != nil || count < 10 {
			t.Fatalf("runtime trust path missing, malformed, or too small: %s (%d): %v", directory, count, err)
		}
		t.Logf("%s: %d public certificates; all eleven canonical denied SPKIs absent", directory, count)
	}
}
