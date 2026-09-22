# -------------------------------------------------------------------------------
#
# Name:        Quizlet plugin for Anki 2.0
# Purpose:     Import decks from Quizlet into Anki 2.0
# Author:
#  - Original: (c) Rolph Recto 2012, last updated 12/06/2012
#              https://github.com/rolph-recto/Anki-Quizlet
#  - Also:     Contributions from https://ankiweb.net/shared/info/1236400902
#  - Current:  JDMaybeMD
# Created:     04/07/2017
#
# Changlog:    Inital release
# * 2023-04-02 parser improvements
# * 2023-02-26 partial shapes support
# * 2022-10-30 add a proxy retry
# * 2022-05-15 add a rich text support
# * 2022-05-12 custom media folder fix (thx, https://github.com/mhujer)
# * 2022-04-20 add an "Add reverse" option
# * 2022-04-18 fix issue with original audio
# * 2022-04-17 fix issue with images/audio
# * 2022-04-10 fix mapping algorithm (thx, https://github.com/mhujer)
# * 2020-09-10 update audio download algorithm
# * 2020-09-08 have fixed audio download for special decks :)
# * 2020-09-06 have fixed a partial import. shame on me :)
# * 2020-09-05 made an audio download optional
# * 2020-09-05 update a quizlet parser

# -------------------------------------------------------------------------------
#!/usr/bin/env python

import re
import json
import urllib.parse
import requests
import webbrowser
import ssl
from aqt.utils import showText
from aqt.qt import *
from aqt import gui_hooks, mw
import os
import sys
import logging
import threading
import time
import urllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookies import SimpleCookie

from anki.collection import AddNoteRequest
from anki.decks import DeckId
from aqt.operations import CollectionOp, QueryOp
try:
    import urllib2
    URLError = urllib2.URLError
except Exception:
    import urllib.request as urllib2
    import urllib.error
    URLError = urllib.error.URLError
    
ADDON_DIR = os.path.dirname(__file__)
VENDOR_DIR = os.path.join(ADDON_DIR, "vendor")

if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

# Suppress noisy TLS client loader logs ("Successfully loaded TLS library") so Anki doesn't treat them as errors. 
tls_logger = logging.getLogger("TLSLibrary")
tls_logger.setLevel(logging.ERROR)
tls_logger.propagate = False
if not tls_logger.handlers:
    tls_logger.addHandler(logging.NullHandler())

try:
    from tls_requests import get as tls_get, HTTPError as TLS_HTTP_ERROR
except ModuleNotFoundError:
    tls_get = requests.get
    from requests.exceptions import HTTPError as TLS_HTTP_ERROR

__window = None

# Anki
requests.packages.urllib3.disable_warnings()

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
}
TLS_IDENTIFIER = "chrome_120"

# Create an SSL context with certificate verification disabled
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE

# Install the SSL context globally
urllib2.install_opener(urllib2.build_opener(urllib2.HTTPSHandler(context=context)))

public_api_key = '0b8aa35d-b521-4fe0-bf0e-2ae07d826acf'

# How many media files to fetch at once. Each tls_requests call builds its own
# session, so these run independently; override with "media_workers" in config.
DEFAULT_MEDIA_WORKERS = 6


def get_cookies():
    config = mw.addonManager.getConfig(__name__)

    if config.get("qlts"):
        return {"qlts": config["qlts"]}

    if config.get("cookies"):
        cookie_parser = SimpleCookie()
        cookie_parser.load(config["cookies"])
        return {key: morsel.value for key, morsel in cookie_parser.items()}

    return {}

# add custom model if needed
def addCustomModel(col):

    # create custom model for imported deck
    mm = col.models
    existing = mm.by_name("Basic Quizlet Extended")
    if existing:
        return existing
    m = mm.new("Basic Quizlet Extended")

    # add fields
    mm.addField(m, mm.newField("FrontText"))
    mm.addField(m, mm.newField("FrontAudio"))
    mm.addField(m, mm.newField("BackText"))
    mm.addField(m, mm.newField("BackAudio"))
    mm.addField(m, mm.newField("Image"))
    mm.addField(m, mm.newField("Add Reverse"))

    # add cards
    t = mm.newTemplate("Normal")

    # front
    t['qfmt'] = "{{FrontText}}\n<br><br>\n{{FrontAudio}}"
    t['afmt'] = "{{FrontText}}\n<hr id=answer>\n{{BackText}}\n<br><br>\n{{Image}}\n<br><br>\n{{BackAudio}}"
    mm.addTemplate(m, t)

    # back
    t = mm.newTemplate("Reverse")
    t['qfmt'] = "{{#Add Reverse}}{{BackText}}\n<br><br>\n{{BackAudio}}{{/Add Reverse}}"
    t['afmt'] = "{{BackText}}\n<hr id=answer>\n{{FrontText}}\n<br><br>\n{{FrontAudio}}\n{{Image}}"
    mm.addTemplate(m, t)

    mm.add(m)
    return m

# throw up a window with some info (used for testing)

def debug(message):
    QMessageBox.information(QWidget(), "Message", message)

def getText(d, text=''):
    if d is None:
        return text
    if d['type'] == 'text':
        text = d['text']
        if 'marks' in d:
            for m in d['marks']:
                if m['type'] in ['b', 'i', 'u']:
                    text = '<{0}>{1}</{0}>'.format(m['type'], text)
                if 'attrs' in m:
                    attrs = " ".join(['{}="{}"'.format(k, v)
                                     for k, v in m['attrs'].items()])
                    text = '<span {}>{}</span>'.format(attrs, text)
        return text
    text = ''.join([getText(c) for c in d['content']]
                   ) if d.get('content') else ''
    if d['type'] == 'paragraph':
        text = '<div>{}</div>'.format(text)
    return text

def ankify(text):
    text = text.replace('\n', '<br>')
    text = text.replace('class="bgY"', 'style="background-color:#fff4e5;"')
    text = text.replace('class="bgB"', 'style="background-color:#cde7fa;"')
    text = text.replace('class="bgP"', 'style="background-color:#fde8ff;"')
    return text

def parseQuizletUrl(url):
    """Work out what a pasted URL points at.

    Returns (deck id, None) on success, or (None, message) to show the user.
    'folder' is returned in place of an id for folder URLs.
    """
    url = url.strip()

    if not url:
        return None, "Enter a Quizlet deck URL"

    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse("https://" + url)

    if "quizlet.com" not in parsed.netloc:
        return None, "That's not a Quizlet URL"

    path = parsed.path.strip("/")

    if not path:
        return None, "Please use the full deck URL"

    if re.search(r'user/', path) and re.search(r'/folders', path):
        if not re.match(r'user/[^/]+/folders/[^/]*', path):
            return None, "That folder URL doesn't look right"
        return 'folder', None

    if not bool(re.search(r'\d', path)):
        return None, "No deck ID found in <i>{0}</i>".format(path)

    return re.search(r"\d+", path).group(0), None


def describeError(exception):
    """One short line for a queue row."""
    if isinstance(exception, QuizletError):
        if exception.code == 403:
            if exception.captcha:
                return "Behind a captcha — try disabling your VPN"
            return "This deck is private"
        if exception.code == 404:
            return "No deck with the ID {0}".format(exception.deck_id)
        return "Download failed"
    return str(exception) or exception.__class__.__name__


def errorDetails(exception):
    """The full text behind a failed row's details button, if there is any."""
    if isinstance(exception, QuizletError):
        return exception.message
    return "{}: {}".format(exception.__class__.__name__, exception)


class QueueEntry:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __init__(self, url, deckID, opts):
        self.url = url
        self.deckID = deckID
        self.opts = opts
        self.status = self.QUEUED
        self.title = self.describeUrl()
        self.detail = "Waiting"
        self.value = None
        self.maximum = None
        self.cards = 0
        self.details = None
        self.cancel = threading.Event()

    def describeUrl(self):
        parsed = urllib.parse.urlparse(self.url)
        if not parsed.scheme:
            parsed = urllib.parse.urlparse("https://" + self.url)
        return parsed.path.strip("/") or self.url

    @property
    def finished(self):
        return self.status in (self.DONE, self.FAILED, self.CANCELLED)


class QueueRow(QWidget):
    """One line of the queue: status glyph, name, progress, and an action button."""

    GLYPHS = {
        QueueEntry.QUEUED: "·",
        QueueEntry.RUNNING: "▸",
        QueueEntry.DONE: "✓",
        QueueEntry.FAILED: "✗",
        QueueEntry.CANCELLED: "✗",
    }

    def __init__(self, entry, onCancel, onDetails):
        super(QueueRow, self).__init__()
        self.entry = entry
        self.onCancel = onCancel
        self.onDetails = onDetails

        self.glyph = QLabel()
        self.glyph.setFixedWidth(14)
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.title = QLabel()
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored,
                                 QSizePolicy.Policy.Preferred)

        self.detail = QLabel()
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Preferred)
        smaller = self.detail.font()
        smaller.setPointSizeF(max(7.0, smaller.pointSizeF() - 1.0))
        self.detail.setFont(smaller)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(3)
        self.bar.hide()

        self.button = QToolButton()
        self.button.setAutoRaise(True)
        self.button.clicked.connect(self.onButton)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        text.addWidget(self.title)
        text.addWidget(self.bar)
        text.addWidget(self.detail)

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 5, 4, 5)
        layout.setSpacing(6)
        layout.addWidget(self.glyph)
        layout.addLayout(text, 1)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        self.setLayout(layout)

        self.refresh()

    def onButton(self):
        if self.entry.finished:
            self.onDetails(self.entry)
        else:
            self.onCancel(self.entry)

    def refresh(self):
        entry = self.entry

        self.glyph.setText(self.GLYPHS.get(entry.status, "·"))
        self.title.setText(entry.title)
        self.detail.setText(entry.detail)

        if entry.finished:
            self.button.setText("…")
            self.button.setToolTip("Show details")
            self.button.setVisible(bool(entry.details))
        else:
            self.button.setText("✕")
            self.button.setToolTip("Cancel")
            self.button.setVisible(True)

        if entry.status == QueueEntry.RUNNING:
            # maximum of 0 makes Qt draw a busy indicator
            self.bar.setMaximum(entry.maximum or 0)
            self.bar.setValue(entry.value or 0)
            self.bar.show()
        else:
            self.bar.hide()


class QuizletWindow(QWidget):
    # main window of Quizlet plugin

    def __init__(self):
        super(QuizletWindow, self).__init__()

        self.config = mw.addonManager.getConfig(__name__)
        self.queue = []
        self.rows = {}
        self.current = None

        self.initGUI()

    # ---------------------------------------------------------------- layout

    def initGUI(self):
        box_url = QHBoxLayout()
        box_url.setSpacing(6)
        self.text_url = QLineEdit("", self)
        self.text_url.setPlaceholderText(
            "https://quizlet.com/150875612/usmle-flash-cards/")
        self.text_url.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.text_url.returnPressed.connect(self.onAdd)
        self.button_add = QPushButton("Add", self)
        self.button_add.clicked.connect(self.onAdd)
        box_url.addWidget(QLabel("Quizlet URL"))
        box_url.addWidget(self.text_url, 1)
        box_url.addWidget(self.button_add)

        self.label_message = QLabel("")
        self.label_message.setWordWrap(True)
        self.label_message.hide()

        self.list_queue = QListWidget(self)
        self.list_queue.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.list_queue.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_queue.setMinimumHeight(170)
        self.list_queue.hide()

        self.label_empty = QLabel(
            "Add a deck or folder URL to start.\nImports run in the background — you can keep using Anki.")
        self.label_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_empty.setMinimumHeight(170)

        box_options = QHBoxLayout()
        box_options.setSpacing(14)
        self.value_download_audio = QCheckBox("Download audio", self)
        self.value_download_audio.setChecked(True)
        self.value_add_reverse = QCheckBox("Add reverse", self)
        self.value_skip_errors = QCheckBox("Skip errors", self)
        self.value_skip_errors.setChecked(True)
        self.value_skip_errors.setToolTip(
            "Leave out audio and images that fail to download, instead of stopping the import")
        box_options.addWidget(self.value_download_audio)
        box_options.addWidget(self.value_add_reverse)
        box_options.addWidget(self.value_skip_errors)
        box_options.addStretch()

        self.advanced_shown = False
        self.button_advanced = QPushButton("▸  Advanced", self)
        self.button_advanced.setFlat(True)
        self.button_advanced.setSizePolicy(QSizePolicy.Policy.Maximum,
                                           QSizePolicy.Policy.Fixed)
        self.button_advanced.clicked.connect(self.onToggleAdvanced)

        box_advanced = QHBoxLayout()
        box_advanced.addWidget(self.button_advanced)
        box_advanced.addStretch()

        self.widget_advanced = self.buildAdvanced()
        self.widget_advanced.hide()

        box_bottom = QHBoxLayout()
        self.button_clear = QPushButton("Clear finished", self)
        self.button_clear.clicked.connect(self.onClearFinished)
        self.button_clear.setEnabled(False)
        self.button_faq = QPushButton("FAQ", self)
        self.button_faq.clicked.connect(self.onFaq)
        box_bottom.addWidget(self.button_clear)
        box_bottom.addStretch()
        box_bottom.addWidget(self.button_faq)

        box_top = QVBoxLayout()
        box_top.setSpacing(10)
        box_top.addLayout(box_url)
        box_top.addWidget(self.label_message)
        box_top.addWidget(self.label_empty)
        box_top.addWidget(self.list_queue, 1)
        box_top.addLayout(box_options)
        box_top.addLayout(box_advanced)
        box_top.addWidget(self.widget_advanced)
        box_top.addLayout(box_bottom)
        self.setLayout(box_top)

        self.setMinimumWidth(520)
        self.setWindowTitle("Import from Quizlet")
        self.text_url.setFocus()
        self.show()

    def buildAdvanced(self):
        """Start/stop phrases and the page-HTML escape hatch, out of the way."""
        self.value_start_phrase = QLineEdit("", self)
        self.value_start_phrase.setPlaceholderText(
            "Start importing from this term. Can be empty")
        self.value_stop_phrase = QLineEdit("", self)
        self.value_stop_phrase.setPlaceholderText(
            "Stop after this term. Can be empty")

        self.value_incoming_html = QTextEdit("", self)
        self.value_incoming_html.setMaximumHeight(90)
        self.value_incoming_html.setPlaceholderText(
            "If an import keeps failing, open the deck page, view its source, and paste it here. "
            "Does not apply to folders.")

        self.button_html = QPushButton("Open deck page", self)
        self.button_html.clicked.connect(self.onHtml)

        box_start = QHBoxLayout()
        box_start.addWidget(QLabel("Start phrase"))
        box_start.addWidget(self.value_start_phrase, 1)

        box_stop = QHBoxLayout()
        box_stop.addWidget(QLabel("Stop phrase"))
        box_stop.addWidget(self.value_stop_phrase, 1)

        box_html_label = QHBoxLayout()
        box_html_label.addWidget(QLabel("Page HTML"))
        box_html_label.addStretch()
        box_html_label.addWidget(self.button_html)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(box_start)
        layout.addLayout(box_stop)
        layout.addLayout(box_html_label)
        layout.addWidget(self.value_incoming_html)

        widget = QWidget(self)
        widget.setLayout(layout)
        return widget

    def onToggleAdvanced(self):
        self.advanced_shown = not self.advanced_shown
        self.button_advanced.setText(
            "▾  Advanced" if self.advanced_shown else "▸  Advanced")
        self.widget_advanced.setVisible(self.advanced_shown)
        self.adjustSize()

    def onHtml(self):
        deckID, error = parseQuizletUrl(self.text_url.text())

        if error:
            self.showMessage(error)
            return

        if deckID == 'folder':
            self.showMessage("Page HTML doesn't work for folders")
            return

        webbrowser.open("https://quizlet.com/{}/flashcards".format(deckID))

    def onFaq(self):
        webbrowser.open(
            "https://github.com/sviatoslav-lebediev/anki-quizlet-importer-extended/wiki/FAQ")

    def showMessage(self, text):
        self.label_message.setText(text or "")
        self.label_message.setVisible(bool(text))

    # ----------------------------------------------------------------- queue

    def snapshotOptions(self):
        """Read everything the import needs off the widgets and the collection.

        The background threads must not touch Qt or mw.col, so every value they
        rely on is captured here, on the main thread, when the entry is queued.
        That also means later edits to these fields don't change what is already
        waiting in the queue.
        """
        return {
            "html": self.value_incoming_html.toPlainText(),
            "download_audio": self.value_download_audio.isChecked(),
            "add_reverse": self.value_add_reverse.isChecked(),
            "skip_errors": self.value_skip_errors.isChecked(),
            "start_phrase": self.value_start_phrase.text(),
            "stop_phrase": self.value_stop_phrase.text(),
            "cookies": get_cookies(),
            "license": self.config.get("license", public_api_key),
            "media_dir": mw.col.media.dir(),
            "media_workers": max(1, int(self.config.get("media_workers", DEFAULT_MEDIA_WORKERS))),
        }

    def onAdd(self):
        url = self.text_url.text().strip()
        deckID, error = parseQuizletUrl(url)

        if error:
            self.showMessage(error)
            return

        if any(entry.url == url and not entry.finished for entry in self.queue):
            self.showMessage("That URL is already in the queue")
            return

        self.showMessage(None)

        entry = QueueEntry(url, deckID, self.snapshotOptions())
        self.queue.append(entry)
        self.addRow(entry)
        self.text_url.clear()
        self.refreshChrome()
        self.pump()

    def pump(self):
        """Start the next queued entry, one at a time."""
        if self.current is not None:
            return

        for entry in self.queue:
            if entry.status == QueueEntry.QUEUED:
                self.startEntry(entry)
                return

    def startEntry(self, entry):
        self.current = entry
        entry.status = QueueEntry.RUNNING
        entry.detail = "Connecting…"
        self.refreshRow(entry)

        progress = ProgressReporter(
            lambda label, value, maximum: self.onProgress(entry, label, value, maximum))

        # no .with_progress() here on purpose: a modal dialog would stop the
        # user adding more URLs while this one downloads
        QueryOp(
            parent=self,
            op=lambda col: fetchEverything(
                entry.url, entry.deckID, entry.opts, progress, entry.cancel),
            success=lambda result: self.onFetched(entry, result),
        ).failure(lambda e: self.onFailed(entry, e)).without_collection().run_in_background()

    def onProgress(self, entry, label, value, maximum):
        if entry.status != QueueEntry.RUNNING:
            return

        entry.detail = label
        entry.value = value
        entry.maximum = maximum
        self.refreshRow(entry)

    def onFetched(self, entry, result):
        if entry.cancel.is_set():
            self.finishEntry(entry, QueueEntry.CANCELLED, "Cancelled")
            return

        decks = result["decks"]

        if not decks:
            if result["failures"]:
                self.finishEntry(entry, QueueEntry.FAILED,
                                 "None of the {0} decks could be downloaded".format(
                                     len(result["failures"])))
            else:
                self.finishEntry(entry, QueueEntry.FAILED, "Nothing to import")
            return

        entry.title = deckTitle(decks[0]) if len(
            decks) == 1 else "{0} decks".format(len(decks))
        entry.cards = sum(len(deck["items"]) for deck in decks)
        entry.detail = "Adding {0} cards…".format(entry.cards)
        entry.value = entry.maximum = None
        self.refreshRow(entry)

        CollectionOp(
            parent=self,
            op=lambda col: addDecksToCollection(col, decks, entry.opts),
        ).success(lambda _changes: self.onAdded(entry, result)).failure(
            lambda e: self.onFailed(entry, e)
        ).run_in_background()

    def onAdded(self, entry, result):
        detail = "{0} cards".format(entry.cards)

        if result["failures"]:
            detail += " · {0} deck(s) skipped".format(
                len(result["failures"]))
            entry.details = "\n\n".join(
                "{0}: {1}".format(f.deck_id, describeError(f)) for f in result["failures"])

        self.finishEntry(entry, QueueEntry.DONE, detail)

    def onFailed(self, entry, exception):
        if isinstance(exception, ImportCancelled):
            self.finishEntry(entry, QueueEntry.CANCELLED, "Cancelled")
            return

        entry.details = errorDetails(exception)
        self.finishEntry(entry, QueueEntry.FAILED, describeError(exception))

    def finishEntry(self, entry, status, detail):
        entry.status = status
        entry.detail = detail
        entry.value = entry.maximum = None
        self.refreshRow(entry)

        if self.current is entry:
            self.current = None

        self.refreshChrome()
        self.pump()

    def onCancel(self, entry):
        entry.cancel.set()

        if entry.status == QueueEntry.QUEUED:
            self.finishEntry(entry, QueueEntry.CANCELLED, "Cancelled")
        else:
            entry.detail = "Cancelling…"
            self.refreshRow(entry)

    def onDetails(self, entry):
        if entry.details:
            showText(entry.details)

    def onClearFinished(self):
        for entry in [e for e in self.queue if e.finished]:
            self.removeRow(entry)
            self.queue.remove(entry)

        self.refreshChrome()

    # ------------------------------------------------------------------ rows

    def addRow(self, entry):
        row = QueueRow(entry, self.onCancel, self.onDetails)
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.list_queue.addItem(item)
        self.list_queue.setItemWidget(item, row)
        self.rows[id(entry)] = (item, row)
        self.list_queue.scrollToItem(item)

    def refreshRow(self, entry):
        pair = self.rows.get(id(entry))

        if pair:
            item, row = pair
            row.refresh()
            item.setSizeHint(row.sizeHint())

    def removeRow(self, entry):
        pair = self.rows.pop(id(entry), None)

        if pair:
            item, _row = pair
            self.list_queue.takeItem(self.list_queue.row(item))

    def refreshChrome(self):
        has_rows = bool(self.queue)
        self.list_queue.setVisible(has_rows)
        self.label_empty.setVisible(not has_rows)
        self.button_clear.setEnabled(
            any(entry.finished for entry in self.queue))

    def shutdown(self):
        """Stop everything; called when the profile closes out from under us."""
        for entry in self.queue:
            entry.cancel.set()

        self.current = None
        self.close()


class ImportCancelled(Exception):
    """Raised on a worker thread when the user closes the progress dialog."""


class QuizletError(Exception):
    # carries the fields the old code read off the QThread, so the error
    # messages the user sees are unchanged
    def __init__(self, deck_id, code=None, captcha=False, message=None):
        super(QuizletError, self).__init__(message or "Quizlet download failed")
        self.deck_id = deck_id
        self.code = code
        self.captcha = captcha
        self.message = message


class ProgressReporter:
    """Throttled progress updates, marshalled back onto the main thread.

    Qt may only be touched from the main thread, and a busy import would
    otherwise post thousands of updates a second at the queue row.
    """

    def __init__(self, sink, interval=0.1):
        self.sink = sink
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def report(self, label, value=None, maximum=None, force=False):
        now = time.monotonic()

        with self.lock:
            if not force and now - self.last < self.interval:
                return
            self.last = now

        sink = self.sink
        mw.taskman.run_on_main(lambda: sink(label, value, maximum))


def checkCancelled(cancel):
    if cancel.is_set():
        raise ImportCancelled()


def ensureTlsLoaded():
    """Load the TLS library up front, on one thread.

    tls_requests caches the ctypes handle on a class attribute the first time a
    client is built; letting a pool of workers race to do that is asking for
    trouble, so we get it out of the way first.
    """
    if tls_get is requests.get:
        return

    try:
        from tls_requests.models.tls import TLSClient
        TLSClient.initialize()
    except Exception as e:
        print("quizlet importer: TLS warm-up failed: {}".format(e))


def deckTitle(deck):
    if "set" in deck:
        return deck['set']['title']
    if "studyable" in deck:
        return deck['studyable']['title']
    return deck['title']


def selectItems(items, startPhrase, stopPhrase):
    """Apply the start/stop phrase window.

    Both the start and the stop item are included, matching the original loop.
    """
    selected = []
    startProcess = False
    stopProcess = False

    for item in items:
        if "".__eq__(startPhrase) or startPhrase == item["term"] or startPhrase == item["definition"]:
            startProcess = True

        if not stopProcess and startProcess:
            selected.append(item)

        if not "".__eq__(stopPhrase) and (stopPhrase == item["term"] or stopPhrase == item["definition"]):
            stopProcess = True

    return selected


def fetchFolderDeckIDs(folderUrl, opts):
    downloader = QuizletDownloader(folderUrl, 'folder', '', opts["cookies"])
    downloader.run()

    if downloader.error:
        raise QuizletError('folder', downloader.errorCode,
                           downloader.errorCaptcha, downloader.errorMessage)

    return re.findall(r'"studyMaterialId":"(\d+)"', downloader.folder_html or '')


def fetchEverything(deckUrl, quizletDeckID, opts, progress, cancel):
    """Scrape the deck(s) and pull down their media. Runs on a worker thread.

    Returns plain dicts carrying the local media filenames, so the CollectionOp
    that follows only has to build notes.
    """
    isFolder = quizletDeckID == 'folder'
    failures = []
    decks = []

    if isFolder:
        progress.report("Reading folder\u2026", force=True)
        deckIDs = fetchFolderDeckIDs(deckUrl, opts)
    else:
        deckIDs = [quizletDeckID]

    for index, deckID in enumerate(deckIDs):
        checkCancelled(cancel)

        if isFolder:
            progress.report("Deck {0} of {1}\u2026".format(index + 1, len(deckIDs)),
                            value=index, maximum=len(deckIDs), force=True)
        else:
            progress.report("Downloading deck\u2026", force=True)

        url = "https://quizlet.com/{}/flashcards".format(deckID)
        downloader = QuizletDownloader(
            url, deckID, '' if isFolder else opts["html"], opts["cookies"])
        downloader.run()

        if downloader.error:
            error = QuizletError(deckID, downloader.errorCode,
                                 downloader.errorCaptcha, downloader.errorMessage)
            # one bad deck shouldn't sink a whole folder, but a single deck
            # import has nothing left to show
            if not isFolder:
                raise error
            failures.append(error)
            continue

        deck = downloader.results
        deck["items"] = selectItems(
            deck["items"], opts["start_phrase"], opts["stop_phrase"])
        decks.append(deck)

    downloadDeckMedia(decks, opts, progress, cancel)

    return {"decks": decks, "failures": failures, "opts": opts}


def downloadDeckMedia(decks, opts, progress, cancel):
    """Fetch every note's media in parallel, recording local filenames on the items."""
    # keyed by (url, suffix) so a file shared by several cards -- typically the
    # same image -- is fetched once and the name handed to every card that wants
    # it, instead of two workers writing the same path at the same time
    jobs = {}

    def addJob(item, field, url, suffix):
        jobs.setdefault((url, suffix), []).append((item, field))

    for deck in decks:
        for item in deck["items"]:
            if opts["download_audio"] and item.get("termAudio"):
                addJob(item, "frontAudioFile", getAudioUrl(item["termAudio"]),
                       str(item["id"]) + "-front.mp3")

            if opts["download_audio"] and item.get("definitionAudio"):
                addJob(item, "backAudioFile", getAudioUrl(item["definitionAudio"]),
                       str(item["id"]) + "-back.mp3")

            if item.get("imageUrl"):
                addJob(item, "imageFile", item["imageUrl"], '')

    if not jobs:
        return

    ensureTlsLoaded()

    def runJob(key):
        url, suffix = key
        file_name = fetchMedia(url, opts, suffix=suffix, fallback=True)

        for item, field in jobs[key]:
            item[field] = file_name

    total = len(jobs)
    done = 0

    with ThreadPoolExecutor(max_workers=opts["media_workers"]) as pool:
        futures = [pool.submit(runJob, key) for key in jobs]

        try:
            for future in as_completed(futures):
                future.result()  # surface whatever a worker raised
                done += 1
                checkCancelled(cancel)
                progress.report("Media {0} of {1}".format(done, total),
                                value=done, maximum=total, force=done == total)
        except BaseException:
            for future in futures:
                future.cancel()
            raise


def addDecksToCollection(col, decks, opts):
    """Create the decks and insert every note, as one undoable operation.

    Runs on the collection thread via CollectionOp, which commits the changes and
    redraws the UI for us -- hence no mw.reset() here.
    """
    target = col.add_custom_undo_entry("Import Quizlet deck")
    model = addCustomModel(col)

    for deck in decks:
        deckID = DeckId(col.decks.id(deckTitle(deck)))
        deck["term_count"] = len(deck["items"])

        notes = []

        for item in deck["items"]:
            note = col.new_note(model)
            note["FrontText"] = ankify(item["term"] or "")
            note["BackText"] = ankify(item["definition"] or "")

            if item.get("frontAudioFile"):
                note["FrontAudio"] = "[sound:" + item["frontAudioFile"] + "]"

            if item.get("backAudioFile"):
                note["BackAudio"] = "[sound:" + item["backAudioFile"] + "]"

            if item.get("imageFile"):
                note["Image"] += '<div><img src="{0}"></div>'.format(
                    item["imageFile"])

            if opts["add_reverse"]:
                note["Add Reverse"] = "True"

            notes.append(AddNoteRequest(note=note, deck_id=deckID))

        if notes:
            col.add_notes(notes)

        # leave the imported deck and note type selected, as before
        col.decks.select(deckID)
        col.models.set_current(model)
        model["did"] = deckID
        col.models.save(model)

    return col.merge_undo_entries(target)


def getAudioUrl(word_audio):
    return word_audio if word_audio.startswith('http') else "https://quizlet.com/{0}".format(word_audio)


def fetchMedia(url, opts, suffix='', fallback=False):
    """Download one media file. Runs on a worker thread, so no Qt in here."""
    url = url.replace('_m', '')
    file_name = "quizlet-" + \
        suffix if suffix else "quizlet-" + url.split('/')[-1]
    fallback_call = False
    request_headers = headers.copy()

    while True:
        try:
            return download_media(url, file_name, request_headers,
                                  opts["cookies"], opts["media_dir"])
        except (urllib2.HTTPError, URLError, TLS_HTTP_ERROR) as e:
            if fallback and not fallback_call:
                fallback_call = True
                url = "https://quizlet-proxy.proto.click/quizlet-media?url={0}".format(
                    urllib.parse.quote(url))
                request_headers["x-api-key"] = opts["license"]
                continue
            if opts["skip_errors"]:
                return None
            else:
                error_code = getattr(e, 'code', None)
                if error_code:
                    print("quizlet importer: throwing exception {}".format(error_code))
                else:
                    print("quizlet importer: throwing exception {}".format(e))
                raise e


def download_media (url, file_name, headers, cookies, media_dir):
    r = tls_get(url, tls_identifier=TLS_IDENTIFIER, headers=headers, cookies=cookies)
    status_code = r.status_code if hasattr(r, "status_code") else r.getcode()

    if status_code != 200:
        # Nothing was written, so returning the name would leave the note
        # pointing at a file that doesn't exist. Raise instead, which lets the
        # caller retry through the proxy and then honour the "skip errors" box.
        if hasattr(r, "raise_for_status"):
            r.raise_for_status()  # 4xx/5xx only

        # anything else that isn't a 200 (3xx we didn't follow, 204, ...)
        raise urllib2.HTTPError(url, status_code, "media download failed",
                                getattr(r, "headers", None), None)

    body = r.content if hasattr(r, "content") else r.read()

    with open(os.path.join(media_dir, file_name), 'wb') as f:
        f.write(body)

    return file_name

def parseTextItem(item):
    return getText(item["richText"], item["plainText"])


def mapItems(studiableItems, setIdToDiagramImage=None):
    result = []

    for studiableItem in studiableItems:
        image = None
        term = None
        term_audio = None
        definition = None
        definition_audio = None

        for side in studiableItem["cardSides"]:
            if (side["label"] == "word"):
                for media in side["media"]:
                    if media["type"] == 4:
                        term_audio = media["url"]

                    if media["type"] == 1:
                        term = parseTextItem(media)

                        if media["ttsUrl"] and term_audio == None:
                            term_audio = media["ttsUrl"]

            if (side["label"] == "definition"):
                for media in side["media"]:
                    if media["type"] == 4:
                        definition_audio = media["url"]

                    if media["type"] == 1:
                        definition = parseTextItem(media)

                        if media["ttsUrl"] and definition_audio == None:
                            definition_audio = media["ttsUrl"]

                    if (media["type"] == 2) and (image == None):
                        image = media["url"]

            # partial shape support
            if (side["label"] == "location"):
                for media in side["media"]:
                    if (media["type"] == 5) and (image == None):
                        image = setIdToDiagramImage[str(
                            studiableItem["studiableContainerId"])]["url"]

        result.append({
            "id": studiableItem["id"],
            "term": term,
            "termAudio": term_audio,
            "definition": definition,
            "definitionAudio": definition_audio,
            "imageUrl": image
        })

    return result


class QuizletDownloader:
    # Scrapes a deck (or folder) off Quizlet. Pure network + parsing, so it is
    # safe to run on a worker thread -- it must never touch Qt or mw.col.

    def __init__(self, url, quizletDeckID, html, cookies):
        self.url = url
        self.results = None
        self.html = html
        self.quizletDeckID = quizletDeckID
        self.cookies = cookies
        self.folder_html = None

        self.error = False
        self.errorCode = None
        self.errorCaptcha = False
        self.errorReason = None
        self.errorMessage = None

    def getDataFromApi(self):
        itemsResponse = None

        try:
            deckUrl = 'https://quizlet.com/webapi/3.9/sets/{0}'.format(
                self.quizletDeckID)
            # TODO download more than 1000 items
            itemsUrl = 'https://quizlet.com/webapi/3.9/studiable-item-documents?filters%5BstudiableContainerId%5D={0}&filters%5BstudiableContainerType%5D=1&perPage={1}&page=1'.format(
                self.quizletDeckID, 1000)

            deckResponse = requests.get(deckUrl, verify=False, headers=headers)
            itemsResponse = requests.get(
                itemsUrl, verify=False, headers=headers)

            rawJson = {"studiableDocumentData": json.loads(
                itemsResponse.text)["responses"][0]["models"]}

            items = mapItems(rawJson)
            title = json.loads(deckResponse.text)["responses"][
                0]['models']['set'][0]['title']

            self.results = {}
            self.results['items'] = items
            self.results['title'] = title
        except Exception as e:
            self.error = True
            self.errorMessage = "{}\n-----------------\n{}".format(
                e, itemsResponse.text if itemsResponse is not None else "")

    def getDataFromPage(self):
        proxyRetry = True

        while True:
            try:
                r = None
                cookies = self.cookies

                page_html = ''

                if self.quizletDeckID == 'folder':
                    url = self.url if proxyRetry else 'https://quizlet-proxy.proto.click/quizlet-folders?url=' + \
                        urllib.parse.quote(self.url, safe='()*!\'')
                    r = tls_get(url, tls_identifier=TLS_IDENTIFIER, headers=headers, cookies=cookies)
                    r.raise_for_status()
                    page_html = r.text
                    self.folder_html = page_html
                else:
                    if self.html:
                        page_html = self.html
                    else:
                        url = self.url if proxyRetry else 'https://quizlet-proxy.proto.click/quizlet-deck?url=' + \
                            urllib.parse.quote(self.url, safe='()*!\'')
                        print(url)
                        r = tls_get(url, tls_identifier=TLS_IDENTIFIER, headers=headers, cookies=cookies)
                        r.raise_for_status()
                        page_html = r.text

                    regex = re.escape('window.Quizlet["setPasswordData"]')

                    if re.search(regex, page_html):
                        if (proxyRetry):
                            proxyRetry = False
                            continue

                        self.error = True
                        self.errorCode = 403
                        return

                    regex = re.escape('window.Quizlet["setPageData"] = ')
                    regex += r'(.+?)'
                    regex += re.escape('; QLoad("Quizlet.setPageData");')
                    m = re.search(regex, page_html)

                    studiableItems = None
                    setIdToDiagramImage = None

                    if not m:
                        regex = re.escape('window.Quizlet["assistantModeData"] = ')
                        regex += r'(.+?)'
                        regex += re.escape('; QLoad("Quizlet.assistantModeData");')
                        m = re.search(regex, page_html)
                        if m:
                            data = json.loads(m.group(1).strip())
                            studiableDocumentData = data['studiableDocumentData']
                            setIdToDiagramImage = studiableDocumentData.get(
                                'setIdToDiagramImage', None)
                            studiableItems = studiableDocumentData.get(
                                'studiableItems', studiableDocumentData.get('studiableItem'))

                    if not m:
                        regex = re.escape('window.Quizlet["cardsModeData"] = ')
                        regex += r'(.+?)'
                        regex += re.escape('; QLoad("Quizlet.cardsModeData");')
                        m = re.search(regex, page_html)
                        if m:
                            data = json.loads(m.group(1).strip())
                            studiableDocumentData = data['studiableDocumentData']
                            setIdToDiagramImage = studiableDocumentData.get(
                                'setIdToDiagramImage', None)
                            studiableItems = studiableDocumentData.get(
                                'studiableItems', studiableDocumentData.get('studiableItem'))

                    if not m:
                        regex = re.escape('dehydratedReduxStateKey":')
                        regex += r'(.+?)'
                        regex += re.escape('},"__N_SSP')
                        m = re.search(regex, page_html)
                        if m:
                            rawData = m.group(1).strip()
                            data = json.loads(json.loads(rawData))
                            studiableItems = data["studyModesCommon"]["studiableData"]["studiableItems"]
                            setIdToDiagramImage = data["studyModesCommon"]["studiableData"]["setIdToDiagramImage"]
                        else:
                            raise Exception("Can't extract data")

                    self.results = {}
                    self.results['items'] = mapItems(
                        studiableItems, setIdToDiagramImage)

                    title = os.path.basename(
                        self.url.strip()) or "Quizlet Flashcards"

                    m = re.search(r'<title[^>]*>(.+?)</title>', page_html, re.IGNORECASE | re.DOTALL)

                    if m:
                        title = m.group(1)
                        title = re.sub(r' \| Quizlet$', '', title)
                        title = re.sub(r'^Flashcards ', '', title)
                        title = re.sub(r'\s+', ' ', title)
                        title = title.strip()

                    self.results['title'] = title

            except TLS_HTTP_ERROR as e:
                if proxyRetry == True:
                    proxyRetry = False
                    continue
                else:
                    self.error = True
                    self.errorCode = e.response.status_code
                    self.errorMessage = e.response.text
                    if "CF-Chl-Bypass" in e.response.headers:
                        self.errorCaptcha = True
            except ValueError as e:
                if proxyRetry == True:
                    continue
                else:
                    self.error = True
                    self.errorMessage = "Invalid json1: {0}".format(e)
            except Exception as e:
                if proxyRetry == True and not self.html:
                    proxyRetry = False
                    continue
                else:
                    self.error = True
                    self.errorMessage = "{}\n-----------------\n{}".format(
                        e, page_html)
            break
        # yep, we got it

    def run(self):
        self.getDataFromPage()

        # the webapi fallback only knows about sets, not folders
        if self.error and self.quizletDeckID != 'folder':
            self.getDataFromApi()

# plugin was called from Anki


def runQuizletPlugin():
    global __window

    # reuse the window so a queue keeps running while it's closed
    if __window is None:
        __window = QuizletWindow()

    __window.show()
    __window.raise_()
    __window.activateWindow()


def onProfileWillClose():
    # the queued options hold a media folder belonging to this profile, and
    # pending CollectionOps would land on whatever opens next
    global __window

    if __window is not None:
        __window.shutdown()
        __window = None


gui_hooks.profile_will_close.append(onProfileWillClose)


# create menu item in Anki
action = QAction("Import from Quizlet", mw)
action.triggered.connect(runQuizletPlugin)
mw.form.menuTools.addAction(action)
