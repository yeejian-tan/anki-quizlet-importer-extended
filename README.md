# Quizlet importer Extended

Upgraded version of the quizlet importer which imports audio files.

<a href="https://www.buymeacoffee.com/moro.programmer" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: auto !important;width: 140px !important;" ></a>

[FAQ](https://github.com/sviatoslav-lebediev/anki-quizlet-importer-extended/wiki/FAQ)

Instead of creating Front and Back items this version creates these fields

    * FrontText
    * FrontAudio
    * BackText
    * BackAudio
    * Image
    * Add Reverse

Note type name is `Basic Quizlet Extended`;

### Importing

Paste a deck or folder URL and press **Add**. Decks are queued and imported one
at a time in the background, so Anki stays usable and you can keep adding URLs
while they download. Each row shows its own progress and can be cancelled; a
finished import lands as a single entry in *Edit → Undo*.

The skip errors checkbox allows to skip media download errors. A file that
can't be downloaded is left out of the note, so cards never reference media
that isn't in your collection.

Under **Advanced**:

* **Start / stop phrase** — import only the part of a deck between two terms.
  Both the start and the stop term are included.
* **Page HTML** — a fallback for when Quizlet blocks the download. Open the deck
  page, view its source, and paste it here. Doesn't apply to folders.

### This addon creates two types of cards: Normal and Reverse

**Normal Template has**:

* Front

    ```html
    {{FrontText}}
    <br><br>
    {{FrontAudio}}
    ```

* Back
    ```html
    {{FrontText}}
    <hr id=answer>
    {{BackText}}
    <br><br>
    {{Image}}
    <br><br>
    {{BackAudio}}
    ```

**Reverse Template is**:

* Front
    ```html
    {{#Add Reverse}}
    {{BackText}}
    <br><br>
    {{BackAudio}}
    {{/Add Reverse}}
    ```

* Back
    ```html
    {{BackText}}
    <hr id=answer>
    {{FrontText}}
    <br><br>
    {{FrontAudio}}
    {{Image}}
    ```

### Fields formats

* FrontAudio - `[sound:"quizlet-CARD_ID-front.mp3"]`
* BackAudio - `[sound:"quizlet-CARD_ID-back.mp3"]`
* Image - `<img src="file_name">`

### Configuration

Set these in Anki via *Tools → Add-ons → Improved Quizlet to Anki 21 Importer → Config*:

* `qlts` / `cookies` - your Quizlet session, for private or login-only decks
* `media_workers` - how many audio/image files to download at once (default `6`).
  Lower it if Quizlet starts rejecting requests; raise it on a fast connection.

Imports run in the background, so Anki stays usable while a deck downloads. Press
`Esc` (or close the progress window) to cancel one. The whole import lands as a
single entry in *Edit → Undo*.

## Repo Activity

![Repo Activity](https://repobeats.axiom.co/api/embed/94e61d46859061470cdf238cbad04e80bcc57300.svg "Repobeats analytics image")
