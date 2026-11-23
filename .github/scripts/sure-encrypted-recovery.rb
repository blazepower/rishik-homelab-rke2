# Cluster-local authenticated recovery; never print the original encryption key or private bytes.
require "openssl"
require "digest"
require "json"
require "stringio"
require "tempfile"

MAGIC = "SUREGCM1".b
HEADER_SIZE = MAGIC.bytesize + 32 + 12

def key_for(secret, salt)
  raise ArgumentError, "missing recovery key" if secret.nil? || secret.bytesize < 32
  OpenSSL::KDF.hkdf(secret, salt: salt, info: "sure-recovery/v1", length: 32, hash: "SHA256")
end

def encrypt(input, path, secret)
  raise ArgumentError, "recovery archive already exists" if File.exist?(path)
  salt = OpenSSL::Random.random_bytes(32)
  nonce = OpenSSL::Random.random_bytes(12)
  header = MAGIC + salt + nonce
  cipher = OpenSSL::Cipher.new("aes-256-gcm")
  cipher.encrypt
  cipher.key = key_for(secret, salt)
  cipher.iv = nonce
  cipher.auth_data = header
  partial = "#{path}.partial"
  File.open(partial, File::WRONLY | File::CREAT | File::EXCL, 0600) do |file|
    file.binmode
    file.write(header)
    while chunk = input.read(1024 * 1024)
      file.write(cipher.update(chunk))
    end
    file.write(cipher.final)
    file.write(cipher.auth_tag)
    file.flush
    file.fsync
  end
  File.rename(partial, path)
end

def decrypt_pass(path, secret)
  File.open(path, "rb") do |file|
    raise ArgumentError, "truncated recovery archive" if file.size < HEADER_SIZE + 16
    header = file.read(HEADER_SIZE)
    raise ArgumentError, "unknown recovery format" unless header.start_with?(MAGIC)
    cipher = OpenSSL::Cipher.new("aes-256-gcm")
    cipher.decrypt
    cipher.key = key_for(secret, header.byteslice(8, 32))
    cipher.iv = header.byteslice(40, 12)
    file.seek(-16, IO::SEEK_END)
    cipher.auth_tag = file.read(16)
    cipher.auth_data = header
    file.seek(HEADER_SIZE)
    remaining = file.size - HEADER_SIZE - 16
    while remaining.positive?
      chunk = file.read([remaining, 1024 * 1024].min)
      raise ArgumentError, "truncated recovery ciphertext" if chunk.nil? || chunk.empty?
      remaining -= chunk.bytesize
      yield cipher.update(chunk)
    end
    yield cipher.final
  end
end

def self_test
  secret = "public-recovery-fixture-key" * 4
  plaintext = "public recovery fixture\n" * 10_000
  file = Tempfile.new("sure-public-recovery")
  path = file.path
  file.close!
  begin
    encrypt(StringIO.new(plaintext), path, secret)
    restored = String.new.b
    decrypt_pass(path, secret) { |chunk| restored << chunk }
    raise "roundtrip mismatch" unless restored == plaintext
    begin
      decrypt_pass(path, "different-public-fixture-key" * 4) { |_chunk| }
      raise "wrong key accepted"
    rescue OpenSSL::Cipher::CipherError
    end
    File.open(path, "r+b") do |io|
      io.seek(-1, IO::SEEK_END)
      byte = io.read(1).getbyte(0)
      io.seek(-1, IO::SEEK_END)
      io.write((byte ^ 1).chr)
    end
    begin
      decrypt_pass(path, secret) { |_chunk| }
      raise "corrupt archive accepted"
    rescue OpenSSL::Cipher::CipherError
    end
  ensure
    File.unlink(path) if File.exist?(path)
  end
  puts JSON.generate({recovery_crypto_self_test: true})
end

begin
  mode, path = ARGV
  if mode == "self-test"
    self_test
  else
    secret = ENV.fetch("SECRET_KEY_BASE")
    case mode
    when "encrypt"
      encrypt(STDIN.binmode, path, secret)
      decrypt_pass(path, secret) { |_chunk| }
      puts JSON.generate({authenticated: true, encrypted_sha256: Digest::SHA256.file(path).hexdigest})
    when "verify"
      decrypt_pass(path, secret) { |_chunk| }
      puts JSON.generate({authenticated: true, encrypted_sha256: Digest::SHA256.file(path).hexdigest})
    when "decrypt"
      # Authenticate the complete archive before releasing any plaintext.
      decrypt_pass(path, secret) { |_chunk| }
      STDOUT.binmode
      decrypt_pass(path, secret) { |chunk| STDOUT.write(chunk) }
    else
      raise ArgumentError, "unknown recovery operation"
    end
  end
rescue StandardError => error
  warn JSON.generate({recovery_error_class: error.class.name})
  exit 2
end
