# Input must already have passed full authenticated recovery verification.
require "rubygems/package"
require "zlib"
require "fileutils"

root = File.realpath("/restore")
Gem::Package::TarReader.new(Zlib::GzipReader.new(STDIN.binmode)) do |archive|
  archive.each do |entry|
    parts = entry.full_name.split("/")
    raise ArgumentError, "unexpected archive path" unless
      %w[pg redis].include?(parts.first) && !parts.include?("..") && !entry.full_name.start_with?("/")
    path = File.join(root, *parts)
    raise ArgumentError, "unexpected existing symbolic link" if File.symlink?(path)
    if entry.directory?
      existed = File.directory?(path)
      FileUtils.mkdir_p(path, mode: 0700)
      # CSI owns pre-existing volume roots; retain their metadata without privileged extraction.
      File.chmod(entry.header.mode & 0777, path) unless existed
    elsif entry.file?
      FileUtils.mkdir_p(File.dirname(path), mode: 0700)
      File.open(path, "wb", 0600) do |file|
        until entry.eof?
          file.write(entry.read(1024 * 1024))
        end
      end
      File.chmod(entry.header.mode & 0777, path)
      File.utime(entry.header.mtime, entry.header.mtime, path)
    else
      raise ArgumentError, "unsupported recovery archive entry"
    end
  end
end
puts "Cluster-local archive extraction complete."
