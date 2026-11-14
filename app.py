"""Fynder — local document search with optional Ollama semantic retrieval."""
import concurrent.futures
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import queue
import time
import traceback
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

SUPPORTED = {'.txt', '.md', '.csv', '.json', '.log', '.html', '.htm', '.pdf', '.docx', '.xlsx'}
APPDATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
DATA = APPDATA / ('FolderLens' if (APPDATA / 'FolderLens').exists() else 'Fynder')
RELEVANCE = {'Broad': 30, 'Balanced': 45, 'Strict': 60}
STOP_WORDS = set('a an the about articles article documents document files file find show me please for of in on to and or is are that with which'.split())

def query_terms(text):
    # Light plural normalization keeps “town” and “towns” together.
    return set(w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w
               for w in re.findall(r'\w+', text.lower()) if w not in STOP_WORDS)

def coverage(terms, path, text):
    found = query_terms(Path(path).stem + ' ' + text)
    return len(terms & found) / len(terms) if terms else 0

def keyword_spans(text, query):
    terms = query_terms(query)
    return [(match.start(), match.end()) for match in re.finditer(r'\w+', text)
            if query_terms(match.group()) & terms]

def extract(path):
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        from pypdf import PdfReader
        return [(f'Page {i+1}', p.extract_text() or '') for i, p in enumerate(PdfReader(path).pages)]
    if suffix == '.docx':
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read('word/document.xml'))
        return [('Document', '\n'.join(''.join(p.itertext()) for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p')))]
    if suffix == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return [(sheet.title, '\n'.join(' | '.join(str(v) if v is not None else '' for v in row) for row in sheet.iter_rows(values_only=True))) for sheet in book]
        finally:
            book.close()
    text = path.read_text(encoding='utf-8', errors='replace')
    if suffix in {'.html', '.htm'}:
        text = re.sub('<[^>]+>', ' ', text)
    return [('Text', text)]

def ollama(route, payload):
    req = urllib.request.Request('http://127.0.0.1:11434/api/' + route, json.dumps(payload).encode(), {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)

def embed(text, model):
    return ollama('embed', {'model': model, 'input': text})['embeddings'][0]

def cosine(a, b):
    if len(a) != len(b):
        return 0
    return sum(x*y for x, y in zip(a, b)) / (math.sqrt(sum(x*x for x in a) * sum(y*y for y in b)) or 1)

class Index:
    def __init__(self, database):
        self.database = str(database)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, stamp TEXT);
            CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, location TEXT, text TEXT, vector TEXT, model TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(path, text, content='chunks', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS add_chunk AFTER INSERT ON chunks BEGIN INSERT INTO search(rowid,path,text) VALUES(new.id,new.path,new.text); END;
            CREATE TRIGGER IF NOT EXISTS remove_chunk AFTER DELETE ON chunks BEGIN INSERT INTO search(search,rowid,path,text) VALUES('delete',old.id,old.path,old.text); END;''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def scan(self, folder, model, status, progress=lambda event: None):
        if not Path(folder).is_dir():
            raise ValueError('The selected folder is unavailable. Reconnect it or choose another folder.')
        seen, errors, count = set(), [], 0
        started = time.monotonic()
        paths = []
        status(f'Scanning folder: {folder}; model={model or "keyword only"}')
        def walk_error(exc):
            raise exc
        for base, dirs, names in os.walk(folder, followlinks=False, onerror=walk_error):
            dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '.venv', '__pycache__'} and not Path(base, d).is_symlink()]
            paths.extend(Path(base, name) for name in names if Path(name).suffix.lower() in SUPPORTED and not Path(base, name).is_symlink())
        total = len(paths)
        status(f'Discovered {total} supported files')
        progress(dict(done=0, total=total, detail='Starting', fraction=0))
        with self.connect() as db:
            for position, path in enumerate(paths):
                    name = path.name
                    file_started = time.monotonic()
                    if path.suffix.lower() not in SUPPORTED or path.is_symlink():
                        continue
                    key = str(path)
                    seen.add(key)
                    try:
                        stat = path.stat()
                        stamp = f'{stat.st_mtime_ns}:{stat.st_size}:{model}'
                        old = db.execute('SELECT stamp FROM files WHERE path=?', (key,)).fetchone()
                        if old and old[0] == stamp:
                            status(f'UNCHANGED {key}')
                            count += 1
                            continue
                        if stat.st_size > 30 * 1024 * 1024:
                            raise ValueError('exceeds 30 MB prototype limit')
                        status('Reading ' + key)
                        progress(dict(done=position, total=total, detail=f'Reading {name}', fraction=position/max(total, 1)))
                        parts = []
                        for location, text in extract(path):
                            for start in range(0, len(text), 1000):
                                chunk = text[start:start+1200].strip()
                                if chunk:
                                    parts.append((key, location, chunk, None, model))
                        if not parts:
                            raise ValueError('no extractable text; scanned PDFs may require OCR')
                        status(f'Extracted {len(parts)} passages from {name}')
                        if model:
                            for number, part in enumerate(parts):
                                detail = f'{name}: embedding passage {number+1}/{len(parts)}'
                                status(f'Ollama request: {detail}; model={model}')
                                progress(dict(done=position, total=total, detail=detail, fraction=(position+number/len(parts))/max(total, 1)))
                                request_started = time.monotonic()
                                vector = json.dumps(embed(part[2], model))
                                parts[number] = (*part[:3], vector, model)
                                status(f'Ollama completed in {time.monotonic()-request_started:.2f}s')
                        with db:
                            db.execute('DELETE FROM chunks WHERE path=?', (key,))
                            db.executemany('INSERT INTO chunks(path,location,text,vector,model) VALUES(?,?,?,?,?)', parts)
                            db.execute('INSERT OR REPLACE INTO files VALUES(?,?)', (key, stamp))
                        count += 1
                        status(f'INDEXED {key}: {len(parts)} passages in {time.monotonic()-file_started:.2f}s')
                    except Exception as exc:
                        with db:
                            db.execute('DELETE FROM chunks WHERE path=?', (key,))
                            db.execute('DELETE FROM files WHERE path=?', (key,))
                        errors.append(f'{name}: {exc}')
                        status(f'ERROR {key}: {traceback.format_exc()}')
                    finally:
                        progress(dict(done=position+1, total=total, detail=name, fraction=(position+1)/max(total, 1)))
            with db:
                for (key,) in db.execute('SELECT path FROM files').fetchall():
                    if key not in seen:
                        status(f'REMOVED from index: {key}')
                        db.execute('DELETE FROM chunks WHERE path=?', (key,))
                        db.execute('DELETE FROM files WHERE path=?', (key,))
        progress(dict(done=total, total=total, detail='Finished', fraction=1))
        status(f'Index complete: {count} searchable files, {len(errors)} skipped; elapsed {time.monotonic()-started:.1f}s')
        return count, errors

    def find(self, query, model='', relevance='Balanced', status=lambda msg: None):
        terms = query_terms(query)
        if not terms:
            return []
        threshold = RELEVANCE[relevance]
        status(f'Relevance filter: {relevance}; minimum score {threshold}/100')
        best, passages = {}, {}
        with self.connect() as db:
            candidates = db.execute('SELECT id,path,location,text,vector,model FROM chunks').fetchall()
            vector = None
            if model:
                status('Embedding search prompt with Ollama')
                vector = embed(query, model)
        missing = 0
        for ident, path, location, text, stored, stored_model in candidates:
            lexical = coverage(terms, path, text)
            semantic = None
            if model:
                if not stored or stored_model != model:
                    missing += 1
                    continue
                passage_vector = json.loads(stored)
                if len(passage_vector) != len(vector):
                    missing += 1
                    continue
                semantic = max(0, min(1, cosine(vector, passage_vector)))
                score = 100 * (.85 * semantic + .15 * lexical)
            else:
                score = 100 * lexical
            if score <= 0 or score < threshold:
                continue
            hit = (ident, path, location, text, score, semantic, lexical)
            passages.setdefault(path, []).append(hit)
            if path not in best or score > best[path][4]:
                best[path] = hit
        if missing:
            status(f'WARNING: {missing} passages lack compatible embeddings. Enable AI and refresh the index to include them.')
        status(f'Relevance scoring: {len(candidates)} passages checked; {len(best)} documents passed; scores use the strongest passage per document')
        return [(*hit, sorted(passages[hit[1]], key=lambda row: (-row[4], row[0]))[:5])
                for hit in sorted(best.values(), key=lambda row: (-row[4], row[1]))[:20]]

class App:
    def __init__(self, root):
        self.root = root
        self.root.title('Fynder')
        self.root.geometry('1100x760')
        self.root.minsize(850, 600)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.index = None
        self.busy = False
        DATA.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.logger = logging.getLogger(f'Fynder.{id(self)}')
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        handler = RotatingFileHandler(DATA / 'debug.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.logger.addHandler(handler)
        self.operation_started = None
        self.root.title('Fynder')
        self.root.geometry(f'{min(1240, root.winfo_screenwidth()-80)}x{min(820, root.winfo_screenheight()-90)}')
        self.root.minsize(1000, 640)
        self.root.configure(background='#ffffff')
        style = ttk.Style()
        style.theme_use('clam')
        ink, muted, accent, line = '#252b2a', '#67706c', '#276653', '#dce0dc'
        style.configure('.', font=('Segoe UI', 10), background='#ffffff', foreground=ink)
        style.configure('TFrame', background='#ffffff')
        style.configure('Side.TFrame', background='#f2f3ef')
        style.configure('TLabel', background='#ffffff', foreground=ink)
        style.configure('Side.TLabel', background='#f2f3ef', foreground=ink)
        style.configure('Muted.TLabel', foreground=muted)
        style.configure('Caption.TLabel', font=('Segoe UI', 9), foreground=muted)
        style.configure('Section.TLabel', background='#f2f3ef', foreground=muted, font=('Segoe UI', 9, 'bold'))
        style.configure('TButton', padding=(10, 6), background='#f5f6f3', borderwidth=1, relief='flat')
        style.map('TButton', background=[('active', '#e6eae4')])
        style.configure('Accent.TButton', background=accent, foreground='white', borderwidth=0)
        style.map('Accent.TButton', background=[('active', '#1f5545'), ('disabled', '#b3bdb6')], foreground=[('disabled', '#f2f3ef')])
        style.configure('TEntry', padding=7, fieldbackground='#ffffff', bordercolor=line, lightcolor=line, darkcolor=line)
        style.configure('TCombobox', padding=5, bordercolor=line, arrowsize=12)
        style.configure('TCheckbutton', background='#ffffff', padding=(0, 4))
        style.configure('Side.TCheckbutton', background='#f2f3ef')
        style.map('Side.TCheckbutton', background=[('active', '#f2f3ef')])
        style.configure('TSeparator', background=line)
        style.configure('Horizontal.TProgressbar', background=accent, troughcolor='#e3e6e0', borderwidth=0, thickness=4)
        style.configure('Treeview', rowheight=44, background='#ffffff', fieldbackground='#ffffff', borderwidth=0, font=('Segoe UI', 10))
        style.configure('Treeview.Heading', background='#f5f6f3', foreground=muted, font=('Segoe UI', 9), padding=(8, 9), relief='flat')
        style.map('Treeview', background=[('selected', '#e3eee7')], foreground=[('selected', '#194d3c')])

        footer = ttk.Frame(root, padding=(16, 7))
        footer.pack(side='bottom', fill='x')
        self.status = tk.StringVar(value='Choose a folder to start searching.')
        ttk.Label(footer, textvariable=self.status, style='Caption.TLabel').pack(side='left')
        ttk.Label(footer, text='Local index · Original files are read-only', style='Caption.TLabel').pack(side='right')
        ttk.Separator(root).pack(side='bottom', fill='x')
        shell = ttk.Frame(root)
        shell.pack(fill='both', expand=True)
        sidebar = ttk.Frame(shell, style='Side.TFrame', width=238, padding=(18, 20))
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text='Fynder', style='Side.TLabel', font=('Segoe UI', 21, 'bold')).pack(anchor='w', pady=(0, 28))
        ttk.Label(sidebar, text='SEARCH FOLDER', style='Section.TLabel').pack(anchor='w')
        self.folder = tk.StringVar(value='No folder selected')
        self.folder_name = tk.StringVar(value='Your documents')
        ttk.Label(sidebar, textvariable=self.folder_name, style='Side.TLabel', font=('Segoe UI', 11, 'bold'), wraplength=198).pack(anchor='w', pady=(12, 4))
        ttk.Label(sidebar, textvariable=self.folder, style='Side.TLabel', font=('Segoe UI', 9), wraplength=195).pack(anchor='w', pady=(0, 12))
        ttk.Button(sidebar, text='Choose folder…', command=self.choose).pack(fill='x')
        ttk.Button(sidebar, text='Refresh index', command=self.scan).pack(fill='x', pady=(6, 18))
        ttk.Separator(sidebar).pack(fill='x', pady=(0, 18))
        ttk.Label(sidebar, text='SEARCH ENGINE', style='Section.TLabel').pack(anchor='w')
        self.semantic = tk.BooleanVar()
        ttk.Checkbutton(sidebar, text='Use local AI', variable=self.semantic, style='Side.TCheckbutton').pack(anchor='w', pady=(8, 8))
        ttk.Label(sidebar, text='Ollama embedding model', style='Side.TLabel', font=('Segoe UI', 9)).pack(anchor='w')
        self.model = tk.StringVar(value='nomic-embed-text')
        ttk.Entry(sidebar, textvariable=self.model).pack(fill='x', pady=(5, 8))
        ttk.Label(sidebar, text='Refresh the index after changing the model or enabling AI.', style='Side.TLabel', font=('Segoe UI', 9), wraplength=195).pack(anchor='w')
        ttk.Separator(sidebar).pack(fill='x', pady=18)
        ttk.Label(sidebar, text='INDEX ACTIVITY', style='Section.TLabel').pack(anchor='w')
        self.progress = ttk.Progressbar(sidebar, mode='determinate', maximum=100)
        self.progress.pack(fill='x', pady=(14, 8))
        self.progress_text = tk.StringVar(value='Ready · No folder indexed')
        ttk.Label(sidebar, textvariable=self.progress_text, style='Side.TLabel', font=('Segoe UI', 9), wraplength=195).pack(anchor='w')
        ttk.Label(sidebar, text='Checks for changes every minute.', style='Side.TLabel', font=('Segoe UI', 9), wraplength=195).pack(anchor='w', pady=(10, 0))
        ttk.Button(sidebar, text='Activity log', command=self.toggle_debug).pack(side='bottom', fill='x')
        ttk.Separator(shell, orient='vertical').pack(side='left', fill='y')

        main = ttk.Frame(shell)
        main.pack(side='left', fill='both', expand=True)
        search_area = ttk.Frame(main, padding=(22, 20, 22, 14))
        search_area.pack(fill='x')
        title_row = ttk.Frame(search_area)
        title_row.pack(fill='x', pady=(0, 12))
        ttk.Label(title_row, text='Search documents', font=('Segoe UI', 14, 'bold')).pack(side='left')
        ttk.Label(title_row, text='Ctrl+K to focus search', style='Caption.TLabel').pack(side='right')
        search_row = ttk.Frame(search_area)
        search_row.pack(fill='x')
        self.query = ttk.Entry(search_row, font=('Segoe UI', 12))
        self.query.pack(side='left', fill='x', expand=True)
        self.query.bind('<Return>', lambda e: self.search())
        ttk.Button(search_row, text='Search', style='Accent.TButton', command=self.search).pack(side='left', padx=(8, 0))
        filters = ttk.Frame(search_area)
        filters.pack(fill='x', pady=(12, 0))
        ttk.Label(filters, text='Relevance', style='Caption.TLabel').pack(side='left')
        self.relevance = tk.StringVar(value='Balanced')
        selector = ttk.Combobox(filters, textvariable=self.relevance, values=list(RELEVANCE), state='readonly', width=11)
        selector.pack(side='left', padx=(8, 14))
        selector.bind('<<ComboboxSelected>>', lambda event: self.search())
        ttk.Label(filters, text='Broad ≥30  /  Balanced ≥45  /  Strict ≥60', style='Caption.TLabel').pack(side='left')
        ttk.Separator(main).pack(fill='x')

        self.debug_panel = ttk.Frame(main, padding=(14, 8))
        self.debug_visible = False
        debug_top = ttk.Frame(self.debug_panel)
        debug_top.pack(fill='x')
        ttk.Label(debug_top, text='Activity log', font=('Segoe UI', 10, 'bold')).pack(side='left')
        ttk.Button(debug_top, text='Hide', command=self.toggle_debug).pack(side='right')
        ttk.Button(debug_top, text='Open log file', command=lambda: os.startfile(str(DATA / 'debug.log'))).pack(side='right', padx=6)
        ttk.Button(debug_top, text='Clear', command=self.clear_debug).pack(side='right')
        self.debug = tk.Text(self.debug_panel, height=6, wrap='word', font=('Consolas', 9), state='disabled', relief='flat', background='#f5f6f3', foreground=ink)
        self.debug.pack(fill='x', pady=(6, 0))
        self.split = ttk.Panedwindow(main, orient='horizontal')
        self.split.pack(fill='both', expand=True)
        left, right = ttk.Frame(self.split), ttk.Frame(self.split)
        self.split.add(left, weight=2)
        self.split.add(right, weight=3)
        self.result_count = tk.StringVar(value='Documents')
        ttk.Label(left, textvariable=self.result_count, font=('Segoe UI', 10, 'bold'), padding=(16, 13)).pack(anchor='w')
        self.results = ttk.Treeview(left, columns=('name', 'score'), show='headings', selectmode='browse')
        self.results.heading('name', text='Document', anchor='w')
        self.results.heading('score', text='Match / 100', anchor='e')
        self.results.column('name', width=230, minwidth=130, stretch=True)
        self.results.column('score', width=92, minwidth=88, stretch=False, anchor='e')
        result_scroll = ttk.Scrollbar(left, command=self.results.yview)
        result_scroll.pack(side='right', fill='y')
        self.results.configure(yscrollcommand=result_scroll.set)
        self.results.pack(fill='both', expand=True)
        self.results.bind('<<TreeviewSelect>>', self.preview)
        self.results.bind('<Double-Button-1>', lambda e: self.open_file())
        reader_bar = ttk.Frame(right, padding=(16, 7))
        reader_bar.pack(fill='x')
        ttk.Label(reader_bar, text='Document preview', font=('Segoe UI', 10, 'bold')).pack(side='left')
        self.open_button = ttk.Button(reader_bar, text='Open original', command=self.open_file, state='disabled')
        self.open_button.pack(side='right')
        self.highlight = tk.BooleanVar(value=True)
        ttk.Checkbutton(reader_bar, text='Highlights', variable=self.highlight, command=self.preview).pack(side='right', padx=10)
        ttk.Separator(right).pack(fill='x')
        reader = ttk.Frame(right)
        reader.pack(fill='both', expand=True)
        self.detail = tk.Text(reader, wrap='word', font=('Segoe UI', 11), spacing1=2, spacing3=6, padx=22, pady=18, relief='flat', borderwidth=0, state='disabled', background='#ffffff', foreground=ink, selectbackground='#d5e6dd')
        self.detail.tag_configure('title', font=('Segoe UI', 16, 'bold'), spacing3=10)
        self.detail.tag_configure('meta', font=('Segoe UI', 9), foreground=muted, spacing3=8)
        self.detail.tag_configure('section', font=('Segoe UI', 10, 'bold'), foreground=accent, spacing1=14, spacing3=8)
        self.detail.tag_configure('relevant', background='#f7edc7', foreground=ink)
        self.detail.tag_configure('keyword', background='#ebd48e', foreground=ink)
        self.detail.tag_raise('keyword')
        self.detail.tag_configure('score_link', foreground=accent, underline=True, font=('Segoe UI', 9))
        self.detail.tag_bind('score_link', '<Button-1>', lambda e: self.show_score_details())
        detail_scroll = ttk.Scrollbar(reader, command=self.detail.yview)
        detail_scroll.pack(side='right', fill='y')
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(fill='both', expand=True)
        self.hits = []
        self.result_query = ''
        self.write('Choose a folder to begin.\n\nSearch by filename, topic, or a question. Matching documents will appear on the left; select one to read its source passages here.\n\nSupported files\nPDF, Word, Excel, text, Markdown, CSV, JSON and HTML.\n\nFor searches by meaning, enable local AI before indexing. Keyword search works without Ollama.')
        root.bind('<Control-k>', lambda e: self.query.focus_set())
        root.bind('<Control-o>', lambda e: self.choose())
        root.bind('<F5>', lambda e: self.scan())
        root.after(100, lambda: self.split.sashpos(0, 360))
        root.after(60000, self.watch)
        self.log('Fynder started. Debug log: ' + str(DATA / 'debug.log'))
        root.after(100, self.drain_events)

    def toggle_debug(self):
        if self.debug_visible:
            self.debug_panel.pack_forget()
        else:
            self.debug_panel.pack(side='bottom', fill='x', before=self.split)
        self.debug_visible = not self.debug_visible

    def log(self, message):
        self.logger.debug(message)
        self.events.put(('log', time.strftime('%H:%M:%S') + ' ' + message))

    def clear_debug(self):
        self.debug.configure(state='normal')
        self.debug.delete('1.0', 'end')
        self.debug.configure(state='disabled')

    def drain_events(self):
        # All Tk operations stay on the UI thread; workers only enqueue events.
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.debug.configure(state='normal')
                self.debug.insert('end', value + '\n')
                if int(self.debug.index('end-1c').split('.')[0]) > 1500:
                    self.debug.delete('1.0', '301.0')
                self.debug.see('end')
                self.debug.configure(state='disabled')
            else:
                self.progress.stop()
                self.progress.configure(mode='determinate', value=value['fraction']*100)
                elapsed = time.monotonic() - self.operation_started if self.operation_started else 0
                self.progress_text.set(f"{value['fraction']:.0%} · {value['done']}/{value['total']} files processed · {elapsed:.0f}s elapsed · {value['detail']}")
        self.root.after(100, self.drain_events)

    def write(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def run(self, operation, done):
        if self.busy:
            return
        self.busy = True
        self.operation_started = time.monotonic()
        future = self.pool.submit(operation)
        def poll():
            if not future.done():
                self.root.after(100, poll)
                return
            self.busy = False
            try:
                done(future.result())
            except Exception as exc:
                self.progress.stop()
                self.progress_text.set('Failed — see debug output')
                self.log(traceback.format_exc())
                self.status.set('Operation failed. See details.')
                if not self.debug_visible:
                    self.toggle_debug()
                messagebox.showerror('Fynder', str(exc))
        poll()

    def choose(self):
        if self.busy:
            return
        folder = filedialog.askdirectory()
        if folder:
            self.folder.set(folder)
            self.folder_name.set(Path(folder).name)
            self.result_count.set('Documents')
            self.open_button.configure(state='disabled')
            ident = hashlib.sha256(folder.lower().encode()).hexdigest()[:20]
            self.index = Index(DATA / (ident+'.sqlite'))
            self.hits = []
            self.results.delete(*self.results.get_children())
            self.scan()

    def scan(self):
        if not self.index or self.busy:
            return
        index, folder = self.index, self.folder.get()
        model = self.model.get().strip() if self.semantic.get() else ''
        self.status.set('Indexing folder…')
        self.progress.configure(mode='indeterminate', value=0)
        self.progress.start(15)
        self.progress_text.set('Discovering files…')
        def done(result):
            count, errors = result
            self.status.set(f'{count} files indexed · {len(errors)} skipped · refreshes every 60 seconds')
            if errors:
                if not self.debug_visible:
                    self.toggle_debug()
                self.write('Files that could not be indexed\n\n'+'\n\n'.join(errors))
        self.run(lambda: index.scan(folder, model, self.log, lambda event: self.events.put(('progress', event))), done)

    def watch(self):
        if self.index and not self.busy:
            self.scan()
        self.root.after(60000, self.watch)

    def search(self):
        if not self.index or self.busy or not self.query.get().strip():
            return
        query = self.query.get().strip()
        model = self.model.get().strip() if self.semantic.get() else ''
        self.status.set('Searching…')
        relevance = self.relevance.get()
        self.log(f'Search started; mode={model or "keyword"}; query length={len(query)} characters')
        search_started = time.monotonic()
        def done(hits):
            self.log(f'Search complete: {len(hits)} documents in {time.monotonic()-search_started:.2f}s')
            self.hits = hits
            self.result_count.set(f'Documents · {len(hits)}')
            self.open_button.configure(state='normal' if hits else 'disabled')
            self.result_query = query
            self.results.delete(*self.results.get_children())
            for row in hits:
                self.results.insert('', 'end', iid=str(len(self.results.get_children())), values=(Path(row[1]).name, f'{row[4]:.1f}'))
            self.status.set(f'{len(hits)} matching documents · {relevance} · '+('hybrid local AI search' if model else 'keyword search'))
            if hits:
                self.results.selection_set('0')
                self.preview()
            else:
                self.write('No documents passed the relevance filter.\n\nTry Broad, a more specific prompt, or enable local AI and refresh the index. Check debug output for missing embeddings.')
        self.run(lambda: self.index.find(query, model, relevance, self.log), done)

    def preview(self, event=None):
        selection = self.results.selection()
        if selection:
            _, path, location, text, score, semantic, lexical, passages = self.hits[int(selection[0])]
            explanation = (f'AI similarity: {semantic:.1%}\nKeyword coverage: {lexical:.1%}\nScore = 85% AI similarity + 15% keyword coverage.'
                           if semantic is not None else f'Keyword coverage: {lexical:.1%}\nAI search is off; score is keyword coverage only.')
            self.write('')
            self.detail.configure(state='normal')
            self.detail.insert('end', Path(path).name + '\n', 'title')
            self.detail.insert('end', f'{score:.1f} / 100 match · {len(passages)} matching passages\n', 'section')
            self.detail.insert('end', str(path) + '\n\n', 'meta')
            self.detail.insert('end', 'How this match is scored\n', 'score_link')
            self.detail.insert('end', 'Matches reflect passages, not a whole-document assessment.\n', 'meta')
            self.detail.insert('end', 'Yellow: matching passages. Darker yellow: query words. Passages may overlap.\n', 'meta')
            self.detail.configure(state='normal')
            for number, passage in enumerate(passages, 1):
                _, _, place, content, strength, _, _ = passage
                self.detail.insert('end', f'{place}  ·  Passage {number}  ·  {strength:.1f} / 100\n', 'section')
                start = self.detail.index('end-1c')
                self.detail.insert('end', content)
                end = self.detail.index('end-1c')
                if self.highlight.get():
                    self.detail.tag_add('relevant', start, end)
                    # Tk counts characters differently on some builds for non-BMP text.
                    for begin, finish in keyword_spans(content, self.result_query):
                        offset = int(self.detail.tk.call('string', 'length', content[:begin]))
                        length = int(self.detail.tk.call('string', 'length', content[begin:finish]))
                        self.detail.tag_add('keyword', f'{start}+{offset}c', f'{start}+{offset+length}c')
                self.detail.insert('end', '\n\n')
            self.detail.configure(state='disabled')
            self.detail.yview_moveto(0)

    def show_score_details(self):
        selection = self.results.selection()
        if not selection:
            return
        hit = self.hits[int(selection[0])]
        semantic, lexical = hit[5], hit[6]
        details = (f'AI similarity: {semantic:.1%}\nKeyword coverage: {lexical:.1%}\n\nScore = 85% AI similarity + 15% keyword coverage.'
                   if semantic is not None else f'Keyword coverage: {lexical:.1%}\n\nLocal AI was off for this search; the score measures keyword coverage only.')
        messagebox.showinfo('Match details', f'{Path(hit[1]).name}\nMatch strength: {hit[4]:.1f} / 100\n\n{details}\n\nThe strongest passage sets the document score. Scores are matching signals, not confidence percentages. Thresholds: Broad 30, Balanced 45, Strict 60.')

    def open_file(self):
        selection = self.results.selection()
        if selection:
            path = self.hits[int(selection[0])][1]
            if Path(path).exists():
                os.startfile(path)
            else:
                messagebox.showinfo('File moved', 'This file has moved or was deleted. Refresh the index.')

if __name__ == '__main__':
    App(tk.Tk()).root.mainloop()
