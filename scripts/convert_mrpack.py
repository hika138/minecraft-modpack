
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source" / "modpack.mrpack"
DEST = ROOT / "docs"

MAX_OVERRIDE_SIZE = 32 * 1024 * 1024
MAX_TOTAL_OVERRIDE_SIZE = 256 * 1024 * 1024


def toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid path: {value!r}")

    if "\\" in value or ":" in value:
        raise ValueError(f"Unsafe path: {value}")

    path = PurePosixPath(value)

    if (
        path.is_absolute()
        or any(part in ("..", ".") for part in value.split("/"))
        or any(not part for part in value.split("/"))
    ):
        raise ValueError(f"Unsafe path: {value}")

    return path


def main():
    if not SOURCE.is_file():
        raise FileNotFoundError(f"Not found: {SOURCE}")

    with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
        out = Path(tmp) / "site"
        out.mkdir()

        entries = {}
        targets = set()

        def write(rel, data, metafile=False):
            rel = safe_path(rel).as_posix()

            if rel in entries:
                raise ValueError(f"Duplicate entry: {rel}")

            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

            entries[rel] = (sha256(data), metafile)

        with zipfile.ZipFile(SOURCE) as archive:
            manifest = json.loads(
                archive.read("modrinth.index.json")
            )

            if manifest.get("formatVersion") != 1:
                raise ValueError("Unsupported mrpack version")

            if manifest.get("game") != "minecraft":
                raise ValueError("Not a Minecraft modpack")

            versions = manifest["dependencies"]
            minecraft = versions.get("minecraft")
            forge = versions.get("forge")

            if minecraft != "1.20.1" or not forge:
                raise ValueError(
                    "Expected Minecraft 1.20.1 Forge"
                )

            # Files installed through download URLs
            for item in manifest["files"]:
                target = safe_path(item["path"])
                env = item.get("env") or {}

                client = env.get("client", "required")
                server = env.get("server", "required")

                if client == "unsupported":
                    continue

                if client not in ("required", "optional"):
                    raise ValueError(f"Invalid client env: {target}")

                if server not in (
                    "required", "optional", "unsupported"
                ):
                    raise ValueError(f"Invalid server env: {target}")

                urls = item.get("downloads") or []
                hash_value = item.get(
                    "hashes", {}
                ).get("sha512", "")

                if not urls or not re.fullmatch(
                    r"[0-9a-fA-F]{128}", hash_value
                ):
                    raise ValueError(
                        f"Missing URL or SHA512: {target}"
                    )

                url = urls[0]
                parsed = urlsplit(url)

                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError(f"Invalid HTTPS URL: {url}")

                rel = target.as_posix()
                if rel in targets:
                    raise ValueError(f"Duplicate target: {rel}")
                targets.add(rel)

                side = (
                    "client"
                    if server == "unsupported"
                    else "both"
                )

                meta = [
                    f"name = {toml_string(target.stem)}",
                    f"filename = {toml_string(target.name)}",
                    f"side = {toml_string(side)}",
                    "",
                    "[download]",
                    f"url = {toml_string(url)}",
                    'hash-format = "sha512"',
                    f"hash = {toml_string(hash_value.lower())}",
                ]

                if client == "optional":
                    meta += [
                        "",
                        "[option]",
                        "optional = true",
                        "default = true",
                    ]

                meta_name = rel + ".pw.toml"
                write(
                    meta_name,
                    ("\n".join(meta) + "\n").encode("utf-8"),
                    metafile=True,
                )

            # Layered overrides: common, then client-specific
            overrides = {}
            total_size = 0

            for prefix in ("overrides/", "client-overrides/"):
                for info in archive.infolist():
                    if not info.filename.startswith(prefix):
                        continue
                    if info.is_dir():
                        continue

                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Symlink not allowed")

                    rel = safe_path(
                        info.filename[len(prefix):]
                    ).as_posix()

                    if rel.lower().endswith(".jar"):
                        raise ValueError(
                            f"Bundled JAR needs manual review: {rel}"
                        )

                    if rel in targets:
                        raise ValueError(
                            f"Download/override conflict: {rel}"
                        )

                    if info.file_size > MAX_OVERRIDE_SIZE:
                        raise ValueError(f"Override too large: {rel}")

                    total_size += info.file_size
                    if total_size > MAX_TOTAL_OVERRIDE_SIZE:
                        raise ValueError(
                            "Total override size exceeds limit"
                        )

                    data = archive.read(info)
                    overrides[rel] = data

            for rel, data in sorted(overrides.items()):
                write(rel, data)

        index = ['hash-format = "sha256"', ""]

        for rel, (file_hash, metafile) in sorted(entries.items()):
            index += [
                "[[files]]",
                f"file = {toml_string(rel)}",
                f"hash = {toml_string(file_hash)}",
                f"metafile = {str(metafile).lower()}",
                "",
            ]

        index_data = (
            "\n".join(index).rstrip() + "\n"
        ).encode("utf-8")

        (out / "index.toml").write_bytes(index_data)

        pack = [
            f"name = {toml_string(manifest['name'])}",
            f"version = {toml_string(manifest['versionId'])}",
            'pack-format = "packwiz:1.1.0"',
            "",
            "[index]",
            'file = "index.toml"',
            'hash-format = "sha256"',
            f'hash = "{sha256(index_data)}"',
            "",
            "[versions]",
            f"minecraft = {toml_string(minecraft)}",
            f"forge = {toml_string(forge)}",
            "",
        ]

        (out / "pack.toml").write_text(
            "\n".join(pack),
            encoding="utf-8",
        )
        (out / ".nojekyll").touch()

        # Replace output only after successful conversion
        if DEST.exists():
            shutil.rmtree(DEST)
        shutil.copytree(out, DEST)

    print(f"Converted: {SOURCE.name}")
    print(f"Files: {len(entries)}")
    print(f"Output: {DEST}")
    print("Success")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
