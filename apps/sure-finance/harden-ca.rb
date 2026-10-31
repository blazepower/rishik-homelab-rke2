# Build public CA overlays only; never inspect private/custom stores or Rails data.
require "openssl"
require "json"
require "set"
require "pathname"
require "fileutils"

module SureTrust
  class InputError < StandardError; end
  PEM = /-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----/m
  SOURCES = "usr/share/ca-certificates/"
  CERTS = "etc/ssl/certs/"

  def self.sha(bytes)
    OpenSSL::Digest::SHA256.hexdigest(bytes)
  end

  def self.policy(text)
    keys = text.lines.map(&:strip)
    unless keys.length == 11 && keys.uniq.length == 11 && keys.all? { |key| /\A[A-F0-9]{64}\z/.match?(key) }
      raise InputError, "expected eleven unique policy keys"
    end
    keys.map(&:downcase).to_set
  end

  def self.certificates(bytes)
    blocks = bytes.scan(PEM)
    remainder = bytes.gsub(PEM, "")
    if blocks.empty? || remainder.include?("-----BEGIN") || remainder.include?("-----END")
      raise InputError, "malformed public PEM input"
    end
    blocks.map do |pem|
      cert = OpenSSL::X509::Certificate.new(pem)
      [pem, sha(cert.public_key.public_to_der), sha(cert.to_der), cert]
    end
  end

  def self.filter(bytes, denied, bundle:)
    before = certificates(bytes)
    output = bytes.gsub(PEM) do |pem|
      denied.include?(sha(OpenSSL::X509::Certificate.new(pem).public_key.public_to_der)) ? "" : pem
    end
    expected = before.reject { |row| denied.include?(row[1]) }
    return [nil, before, []] if expected.empty? && !bundle
    after = certificates(output)
    if (bundle && after.length < 10) ||
       after.map { |row| row[2] }.tally != expected.map { |row| row[2] }.tally ||
       after.any? { |row| denied.include?(row[1]) }
      raise InputError, "unaffected-certificate preservation failed"
    end
    [output, before, after]
  end

  def self.destination(path, manifest)
    return File.join("os-sources", path.delete_prefix(SOURCES)) if path.start_with?(SOURCES)
    return File.join("os-certs", path.delete_prefix(CERTS)) if path.start_with?(CERTS)
    return "aws.crt" if path == manifest.fetch("bundles").fetch("aws")
    return "skylight.pem" if path == manifest.fetch("bundles").fetch("skylight")
    raise InputError, "unexpected public store path"
  end

  def self.resolve(path, entries, visited = Set.new)
    raise InputError, "cyclic public CA alias" if visited.include?(path)
    record = entries.fetch(path) { raise InputError, "unknown public CA alias" }
    return path if record.fetch("kind") == "file"
    target = record.fetch("target")
    relative = target.start_with?("/") ? target.delete_prefix("/") : File.join(File.dirname(path), target)
    resolve(Pathname.new(relative).cleanpath.to_s, entries, visited | [path])
  end

  def self.prepare(manifest, denied, output, root: "/")
    entries = manifest.fetch("entries")
    bundles = manifest.fetch("bundles").values.to_set
    filtered = {}
    summary = {}
    entries.each do |path, record|
      unless Pathname.new(path).cleanpath.to_s == path && !path.start_with?("/")
        raise InputError, "invalid public CA path"
      end
      destination(path, manifest)
      input = File.join(root, path)
      stat = File.lstat(input)
      if record.fetch("kind") == "link"
        unless stat.symlink? && File.readlink(input) == record.fetch("target")
          raise InputError, "public CA alias differs from factory"
        end
        resolve(path, entries)
      else
        unless record.fetch("kind") == "file" && stat.file? && !stat.symlink?
          raise InputError, "unsupported public CA input"
        end
        bytes = File.binread(input)
        raise InputError, "public CA input differs from pinned factory" unless sha(bytes) == record.fetch("sha256")
        clean, before, after = filter(bytes, denied, bundle: bundles.include?(path))
        filtered[path] = clean
        summary[path] = {before: before.length, after: after.length} if bundles.include?(path)
        next unless bundles.include?(path)
        store = OpenSSL::X509::Store.new
        after.each { |row| store.add_cert(row[3]) }
        rejected = before.select { |row| denied.include?(row[1]) }
        positive = after.find { |row| row[3].not_before <= Time.now && row[3].not_after >= Time.now && row[3].issuer == row[3].subject }
        if positive.nil? || !store.verify(positive[3]) || rejected.any? { |row| store.verify(row[3]) }
          raise InputError, "native public-root rejection/positive control failed"
        end
      end
    end
    unless File.directory?(output) && !File.symlink?(output)
      raise InputError, "dedicated output volume required"
    end
    allowed = %w[os-certs os-sources aws.crt skylight.pem]
    raise InputError, "unexpected overlay output" unless (Dir.children(output) - allowed).empty?
    allowed.each do |name|
      path = File.join(output, name)
      raise InputError, "symlinked output root" if File.symlink?(path)
      FileUtils.remove_entry(path) if File.exist?(path)
    end
    filtered.each do |path, bytes|
      next if bytes.nil?
      target = File.join(output, destination(path, manifest))
      FileUtils.mkdir_p(File.dirname(target), mode: 0o755)
      File.binwrite(target, bytes)
      File.chmod(0o644, target)
      raise InputError, "overlay readback failed" unless File.binread(target) == bytes
    end
    entries.each do |path, record|
      next unless record.fetch("kind") == "link"
      next if filtered.fetch(resolve(path, entries)).nil?
      target = File.join(output, destination(path, manifest))
      FileUtils.mkdir_p(File.dirname(target), mode: 0o755)
      File.symlink(record.fetch("target"), target)
    end
    summary
  end

  def self.main
    tools = File.dirname(__FILE__)
    manifest = JSON.parse(File.read(File.join(tools, "factory-ca.json")))
    denied = policy(File.read(File.join(tools, "blocked-spkis.sha256")))
    unless ARGV.empty? && OpenSSL::X509::DEFAULT_CERT_FILE == manifest.fetch("default_file") &&
           OpenSSL::X509::DEFAULT_CERT_DIR == manifest.fetch("default_dir") &&
           !ENV.key?("SSL_CERT_FILE") && !ENV.key?("SSL_CERT_DIR")
      raise InputError, "unexpected native default trust selection"
    end
    manifest.fetch("default_aliases").each do |path, target|
      raise InputError, "native default CA alias changed" unless File.readlink("/" + path) == target
    end
    summary = prepare(manifest, denied, "/ca-output")
    puts JSON.generate({sure_public_ca_overlay_verified: true, bundles: summary})
  end
end

if $PROGRAM_NAME == __FILE__
  begin
    SureTrust.main
  rescue SureTrust::InputError, JSON::ParserError, OpenSSL::OpenSSLError, SystemCallError => error
    warn "Sure public CA hardening failed (#{error.class}); refusing application startup."
    exit 78
  end
end
