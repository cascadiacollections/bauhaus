"""Sign image metadata with a PGP/GPG key."""

import json
import subprocess
import tempfile
from pathlib import Path


def sign_metadata(
    metadata: dict | str,
    key_id: str | None = None,
    passphrase: str | None = None,
) -> bytes | None:
    """Create a detached GPG signature for metadata JSON.

    Args:
        metadata: Metadata dict or JSON string to sign. Dicts are serialised
                  with sorted keys for a stable, reproducible byte representation.
        key_id: GPG key ID or fingerprint to use for signing.  If *None* the
                GPG default signing key is used.
        passphrase: Key passphrase, if required (e.g. in CI environments).

    Returns:
        Detached signature bytes on success, or *None* when GPG is unavailable
        or signing fails (errors are printed to stderr but never raised).
    """
    if isinstance(metadata, dict):
        metadata_json = json.dumps(metadata, indent=2, sort_keys=True)
    else:
        metadata_json = str(metadata)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = Path(tmpdir) / "metadata.json"
            sig_path = Path(tmpdir) / "metadata.json.sig"
            meta_path.write_text(metadata_json, encoding="utf-8")

            cmd = [
                "gpg",
                "--batch",
                "--yes",
                "--output", str(sig_path),
                "--detach-sign",
            ]
            if passphrase:
                cmd += ["--passphrase", passphrase, "--pinentry-mode", "loopback"]
            if key_id:
                cmd += ["--local-user", key_id]
            cmd.append(str(meta_path))

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                print(
                    f"  ⚠ GPG signing failed (exit {result.returncode}): "
                    f"{result.stderr.decode(errors='replace').strip()}",
                )
                return None

            return sig_path.read_bytes()

    except FileNotFoundError:
        print("  ⚠ GPG not found — metadata will not be signed")
        return None
    except subprocess.TimeoutExpired:
        print("  ⚠ GPG signing timed out — metadata will not be signed")
        return None
    except OSError as exc:
        print(f"  ⚠ GPG signing error: {exc}")
        return None
