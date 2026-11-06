using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;

internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length != 2 || (args[1] != "factory" && args[1] != "filtered"))
            throw new ArgumentException("Expected public fixture directory and factory|filtered");

        const string vendorDirectory = "/app/readarr/bin/";
        if (Environment.Version.ToString() != "6.0.36" ||
            typeof(object).Assembly.Location != vendorDirectory + "System.Private.CoreLib.dll" ||
            Environment.ProcessPath != vendorDirectory + "Readarr")
            throw new InvalidOperationException("Probe is not using the expected vendor apphost/CLR");

        using JsonDocument fixtures = JsonDocument.Parse(
            File.ReadAllText(Path.Combine(args[0], "cases.json")));
        var rows = new List<object>();
        bool successful = true;
        DateTime verificationTime = DateTime.Parse(
            fixtures.RootElement.GetProperty("verification_time_utc").GetString()!,
            CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal);
        foreach (JsonElement item in fixtures.RootElement.GetProperty("cases").EnumerateArray())
        {
            string fingerprint = item.GetProperty("certificate_sha256").GetString()!;
            using var certificate = new X509Certificate2(
                File.ReadAllBytes(Path.Combine(args[0], fingerprint + ".der")));
            if (Convert.ToHexString(SHA256.HashData(certificate.RawData)) != fingerprint)
                throw new InvalidOperationException("Public certificate fixture hash mismatch");

            using var chain = new X509Chain();
            chain.ChainPolicy.TrustMode = X509ChainTrustMode.System;
            chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            chain.ChainPolicy.DisableCertificateDownloads = true;
            chain.ChainPolicy.VerificationTime = verificationTime;
            bool trusted = chain.Build(certificate);
            X509ChainStatusFlags flags = chain.ChainStatus.Aggregate(
                X509ChainStatusFlags.NoError, (value, status) => value | status.Status);
            bool expectedTrust = args[1] == "factory" || !item.GetProperty("blocked").GetBoolean();
            const X509ChainStatusFlags trustFailures =
                X509ChainStatusFlags.UntrustedRoot | X509ChainStatusFlags.PartialChain;
            bool accepted = expectedTrust
                ? trusted && flags == X509ChainStatusFlags.NoError
                : !trusted && flags != X509ChainStatusFlags.NoError && (flags & ~trustFailures) == 0;
            successful &= accepted;
            rows.Add(new {
                certificate_sha256 = fingerprint,
                spki_sha256 = item.GetProperty("spki_sha256").GetString(),
                expected_trust = expectedTrust,
                trusted,
                status = chain.ChainStatus.Select(status => status.Status.ToString()).ToArray(),
                accepted
            });
        }
        Console.WriteLine(JsonSerializer.Serialize(new {
            mode = args[1],
            framework = RuntimeInformation.FrameworkDescription,
            core_library = typeof(object).Assembly.Location,
            apphost = Environment.ProcessPath,
            verification_time_utc = verificationTime.ToString("O", CultureInfo.InvariantCulture),
            successful,
            cases = rows
        }));
        return successful ? 0 : 1;
    }
}
