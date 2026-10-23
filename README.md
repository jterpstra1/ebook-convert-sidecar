# ebook-convert-sidecar

Converts the books in a library folder to extra formats with Calibre's `ebook-convert`, and saves
the result next to the original:

```
Author/Title/Title.epub   ->   Author/Title/Title.azw3
```

It runs as a container next to your library app and only touches files. Apps that treat
same-named files in one folder as one book in several formats pick the new file up as an extra
format of the existing book, with no changes to the app. Tested with
[Grimmory](https://grimmory.org) and [BookOrbit](https://bookorbit.app).

## How it works

- Walks the library and, for every book in a source format (default `epub`), creates each target
  format (default `azw3`) that is missing in that folder.
- **Keeps converted files in sync.** When the source changes after a conversion (for example your
  library app writing edited metadata or a new cover into the EPUB), the converted file is made
  again on the next run.
- Only replaces files it created itself. A `Title.azw3` you put there yourself is never touched.
  Converted files that your library app moves or renames are still recognised.
- Converts in a scratch dir, copies the result into the book's folder as a hidden `.part` file
  (which library apps ignore) and renames it into place, so they never see half a file.
- Keeps its bookkeeping in `/config/state.json`: which files it created, from which version of
  the source, and which conversions failed. A failed conversion is retried once the source
  changes, the Calibre plugins change, or you remove its entry from `failed`.
- Skips hidden folders, and folders containing an `.ignore` file (configurable).
- Starts in **dry-run** mode: it only logs what it would create.

## What your library app needs

1. **Same-named files in one folder are one book.** Grimmory and BookOrbit both do this, unless
   the library is set to *Book per file*: then every file becomes its own book and the converted
   file shows up as a duplicate.
2. **It notices new files**, through folder watching or a scheduled scan.
3. **It knows the target format.** Formats your app can't read are written but ignored by it.
4. To keep converted files in sync with metadata you edit in the app, the app has to **write
   metadata into the EPUB file**. Otherwise edits only live in the app's database and the EPUB
   never changes.

| | Grimmory | BookOrbit |
|---|---|---|
| Grouping | *Auto detect* or *Book per folder* | Any mode except *Book per file* |
| Noticing new files | *Folder monitoring* on the library | *Watch folders* or an *Auto-scan schedule* on the library |
| Writing metadata into files | *Write metadata to file* for EPUB | *Write to file* on the library |
| File owner | Usually `99:100` on Unraid | `PUID`/`PGID` of the BookOrbit container (default `1000:1000`) |

## Settings

| Variable | Default | |
|---|---|---|
| `DRY_RUN` | `true` | `false` to actually convert |
| `TARGET_FORMATS` | `azw3` | Comma-separated, e.g. `azw3,mobi,pdf` |
| `SOURCE_FORMATS` | `epub` | Comma-separated, in order of preference when a book has several |
| `INTERVAL_MINUTES` | `0` | Minutes between runs; `0` runs once and exits |
| `MAX_PER_RUN` | `0` | Stop after this many conversions per run (`0` = no limit) |
| `EXTRA_ARGS` | | Extra `ebook-convert` arguments, e.g. `--output-profile kindle_pw3` |
| `CONVERT_TIMEOUT` | `600` | Seconds before one conversion is given up |
| `PUID` / `PGID` | `99` / `100` | Owner of the files it writes. Use the same as your library app |
| `UMASK` | `002` | Files are readable by everyone, writable by owner and group. Set `000` if other users on the share must be able to change them too |
| `IGNORE_FILES` | `.ignore` | Comma-separated file names; a folder containing one is skipped |
| `TZ` | | Timezone for log timestamps |

Mounts: the library at `/books`, a small state dir at `/config`, and optionally Calibre plugins
at `/plugins`. The container starts as root only to switch to `PUID`/`PGID`, and needs no
network.

## Calibre plugins

Any Calibre plugin that works with `ebook-convert` can be used: plugins that add an output
format, or *File type* plugins that process a book before it is converted.

1. Put the plugin's `.zip` (the same file you would load in Calibre via *Preferences > Plugins >
   Load plugin from file*) in the folder mounted at `/plugins`.
2. If the plugin needs settings, configure it in a desktop Calibre once, then copy its settings
   file from Calibre's config folder (*Preferences > Miscellaneous > Open calibre configuration
   directory*, then `plugins/<Plugin Name>.json`) next to the zip.
3. Restart the container. The log shows `Installed Calibre plugin …` for each zip.

Plugins are installed into a fresh Calibre config on every start, so the folder is the complete
list: delete a zip and that plugin is gone after the next restart. When the contents of the
folder change, conversions that failed before are retried once, since a plugin may fix them.

Plugins run inside the container with access to your library, so only add ones you trust, and
you are responsible for what they do to your books.

## Unraid

1. In the Unraid terminal (`>_` in the web UI), download the template:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-ebook-convert-sidecar.xml https://raw.githubusercontent.com/jterpstra1/unraid-templates/main/ebook-convert-sidecar.xml
   ```
2. In **Docker > Add Container**, pick **ebook-convert-sidecar** under *User templates*. Point
   **Library** at the same share your library app uses (e.g. `/mnt/user/books/`), set
   **PUID**/**PGID** to the owner of those files, and keep **Dry Run** `true` and **Max per
   run** `5`.
3. Start it and read the log. If the planned files look right, set **Dry Run** to `false` and
   start it again. Check in your library app that the new formats show up on the books.
4. Once happy, set **Max per run** to `0` and **Interval** to e.g. `60` to keep converting new
   books as they arrive.

## Running elsewhere

```bash
docker run --rm -v /path/to/books:/books -v ./config:/config \
  -e DRY_RUN=false -e PUID=1000 -e PGID=1000 ghcr.io/jterpstra1/ebook-convert-sidecar
```

## Development

```bash
python3 -m unittest -v test_convert
```

The tests use a fake converter. With Calibre installed locally you can run `convert.py` directly
against a test folder: `LIBRARY_DIR=./books STATE_DIR=./state DRY_RUN=false python3 convert.py`.
