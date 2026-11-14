# Fynder

A Windows desktop prototype for searching a selected folder. Documents stay on your computer.

## Run

Install Python 3.11 or newer with Tcl/Tk enabled, then run:

```
python -m pip install -r requirements.txt
python app.py
```

Or double-click **Start Fynder.cmd** after installing dependencies.

Choose a folder, wait for indexing, and search. Results display original source passages and file paths. Double-click a result to open the file in its default application.

## Progress and debugging

The progress bar shows discovery activity, then the percentage and number of supported files processed. During AI indexing it also advances through each file's embedding passages. Percentage reflects file progress, not a time estimate: large documents can take much longer than small ones. Elapsed time and the current operation appear beneath the bar.

The Debug output panel shows extracted passage counts, unchanged files, individual Ollama request timings, completed files, errors with tracebacks, and search timings. **Open log** opens `%LOCALAPPDATA%\Fynder\debug.log`. Logs rotate at 2 MB with three backups. **Clear view** clears only the on-screen output. Logs include file paths and error details; document contents and search query text are not deliberately logged.

## Optional local AI

Install Ollama separately, start it, and download an embedding model:

```
ollama pull nomic-embed-text
```

Enable **Local AI search** before indexing. Refresh after changing the model. Embeddings are requested only from localhost:11434. No cloud service or API key is used. The first index can take some time.

## Relevance control and scores

The result preview shows up to five highest-scoring passages from each document, with page/sheet labels and scores. Matching passages are highlighted pale yellow; exact query words (with basic plural normalization) appear in orange. Use the highlight checkbox to turn coloring off. Semantic highlighting applies to the retrieved passage as a whole, not individually scored sentences. Passages can overlap. This preview uses the indexed text and never edits or annotates the original PDF or document. No reindexing is required for highlighting.

Each result now represents one document and shows its strongest matching passage, plus a match strength out of 100. Select **Broad** (minimum 30), **Balanced** (45, default), or **Strict** (60) to filter results. Changing the setting reruns the current search when the app is idle. No reindex is needed for this control.

With local AI enabled, the score is 85% nonnegative cosine similarity plus 15% coverage of meaningful query terms. Without AI, it measures query-term coverage only. Common filler words are ignored and basic plurals are normalized. Both retrieval modes apply their score cutoff to all results. The detail panel shows the score components.

These are heuristic matching signals, not percentages of confidence or a judgment about the entire article. Thresholds are initial defaults and are not calibrated against a labeled dataset. A passing mention can still produce a strong passage match. In AI mode, passages without embeddings for the selected model are excluded and reported in debug output; refresh with AI enabled to include them. Up to 20 documents are returned.

Run deterministic relevance checks with `python -m unittest test_relevance.py` from this folder. Tests use simulated embeddings and do not require Ollama.

## Prototype scope

- Keyword coverage search plus optional semantic search with explicit relevance scores and filters.
- PDF text, Word DOCX, Excel XLSX (cached values), and common text formats.
- Incremental refresh every 60 seconds while the app is open; deleted files are removed from the index.
- Local SQLite index in `%LOCALAPPDATA%\Fynder`. Delete that directory with the app closed to remove cached text and vectors.
- Files over 30 MB and unreadable files are reported as skipped. Scanned PDFs need OCR and may contain no searchable text. Password-protected documents are not supported.
- No answer generation yet: results show source excerpts directly. Opening a PDF uses the default viewer and does not navigate to the page automatically.
- Initial version uses a linear vector scan, suitable for modest personal folders. Large collections will need a dedicated vector index.
- Folder selection is per session. This is source code, not a packaged installer.

## Desktop interface

The sidebar contains folder selection, Ollama settings, and indexing activity. The resizable document table shows match scores; the reading pane shows highlighted source passages. Click **How this match is scored** for the score breakdown. **Activity log** opens a collapsible debug drawer; errors open it automatically. Keyboard shortcuts: Ctrl+K focuses search, Ctrl+O selects a folder, F5 refreshes the index.

New installations store their local data under `%LOCALAPPDATA%\Fynder`. Existing installations continue using `%LOCALAPPDATA%\FolderLens` when that folder exists, so previously generated embeddings are preserved. The old launcher remains as a compatibility shortcut.
