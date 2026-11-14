
# Clean-Auto-Subs

**Version 1.0.0 — Public beta**

A yt-dlp plugin that cleans up YouTube's English auto-generated captions by removing rolling repetition, rebuilding subtitle cues, and improving line breaks using source timing.

> Clean-Auto-Subs works with captions YouTube has already generated. It does not transcribe audio, translate dialogue, or correct recognition mistakes.

<div align="center">
https://github.com/user-attachments/assets/b9c8fdcb-d312-4399-a033-060eaa64671a
</div>

## Highlights

- Removes repeated text from rolling automatic captions
- Rebuilds cues around sentences, phrases, and speech regions
- Uses English grammar heuristics to improve cue and line breaks
- Captures inline WebVTT timestamps before converting subtitles to SRT
- Uses local source timing to refine suitable cue boundaries
- Removes descriptive bracketed annotations such as `[Music]`
- Preserves spoken-word censorship placeholders such as `[ __ ]`
- Converts YouTube `>>` speaker markers into readable dash dialogue
- Reports timing, cleanup, and readability results in the terminal/log
- Requires no external language model, NLP service, or python dependencies

## Requirements

- yt-dlp & FFmpeg installed
- English automatic captions available for the selected video

If running `yt-dlp` from source or `pip`, use a Python version supported by your yt-dlp release. See the [yt-dlp requirements](https://github.com/yt-dlp/yt-dlp#dependencies).

## Quick start

1. Install the plugin by placing `Clean-Auto-Subs.py` inside this folder structure...

```text
%APPDATA%/
└── yt-dlp/
    └── plugins/
        └── cleanautosubs/
            └── yt_dlp_plugins/
                └── postprocessor/
                    └── Clean-Auto-Subs.py
```

2. Run a download test with this command, replacing `VIDEO_URL` with a YouTube video URL:

```sh
yt-dlp --write-auto-subs --sub-langs "en-orig" --sub-format "vtt" --use-postprocessor "CleanAutoSubs:when=before_dl" "VIDEO_URL"
```

This downloads the video, with English automatic captions in WebVTT format. This plugin converts and cleans the selected subtitles track(s) to SRT.

Check the terminal output for a `CleanAutoSubs` version summary and a `Timing source:` line. Play the result and review the captions before processing a larger batch.

Running `python Clean-Auto-Subs.py` does not process a subtitle file. This module is loaded and called by yt-dlp.

<details>
<summary><h2>Usage options</h2></summary>

Instead of typing long commands every time, you can save default options in a `config.txt` file inside your `yt-dlp` configuration folder—right next to your `plugins` directory:

* **Windows:** `%APPDATA%\yt-dlp\config.txt`
* **macOS / Linux:** `~/.config/yt-dlp/config`

Once saved, running `yt-dlp "VIDEO_URL"` automatically applies your settings.

---
Below is an example configuration that downloads a video, cleans and embeds English automatic captions, labels the subtitle track, and includes optional quality-of-life defaults. [change paths for local filesystem]

```ini
# Creates an ongoing list of downloaded videos, prevents re-downloading
--download-archive "D:/YouTube/download-archive.txt"

# Temporary location, destination & file naming preferences
-P "temp:."
-P "home:D:/YouTube"
-o "%(channel)s/%(fulltitle)s.%(ext)s"

# Language, format & container preferences
-f "bv*+ba[language^=en]/bv*+ba/b"
-S "res:1080,vcodec:avc,acodec:m4a,proto,vbr,abr"
-t mkv

# Clean & embed English automatic captions
--write-auto-subs
--sub-langs "en-orig"
--sub-format "vtt"
--use-postprocessor "CleanAutoSubs:when=before_dl"
--embed-subs
--ppa 'EmbedSubtitle+ffmpeg_o:-metadata:s:s:0 title="Cleaned Auto Captions"'

# Optional Defaults: embed video metadata, chapters, and thumbnail
--convert-thumbnails jpg
--embed-thumbnail
--embed-metadata
--embed-chapters
```
---

### Subtitle selection tip

* **Prefer creator subtitles when available:** Add `--write-subs` alongside `--write-auto-subs`. When a video has creator-authored subtitles for your requested language, `yt-dlp` selects those instead and `CleanAutoSubs` leaves them untouched.

See the [yt-dlp option reference](https://github.com) for additional configuration and subtitle options.

</details>


<details>
<summary><h2>How cleanup works</h2></summary>

| Stage | Action |
|---|---|
| 1 | Identify selected automatic-caption tracks |
| 2 | Capture available inline WebVTT timing before conversion |
| 3 | Convert selected subtitle files to SRT through FFmpeg |
| 4 | For English automatic captions, select usable VTT timing or the SRT fallback |
| 5 | Remove rolling repetition and repair source overlaps |
| 6 | Strip descriptive bracket annotations while preserving censorship markers |
| 7 | Group speech into regions and rebuild cue boundaries |
| 8 | Refine suitable boundaries using nearby exact VTT timing |
| 9 | Repair short or fast fragments and normalize display timing |
| 10 | Rewrap lines and render speaker markers as dashes |
| 11 | Write the cleaned SRT and report structural and readability metrics |

### How cue boundaries are scored

Inside each continuous speech region, Clean-Auto-Subs uses a dynamic-programming search to evaluate possible subtitle splits and choose the lowest-cost sequence of cues. Every candidate cue starts with a small base cost to discourage rapid-fire fragments, then accumulates penalties or bonuses based on readability and English grammar:

* **Sentence & clause boundaries (strongest bonuses):** Ending a cue at sentence punctuation (`.`, `!`, `?`) provides the largest bonus, while commas, semicolons, and colons provide a moderate bonus. Leaving a completed sentence buried in the middle of a cue carries a heavy penalty.
* **Stranded grammar & fixed phrases (heavy penalties):** Splitting right after determiners (`the`, `a`), subject pronouns (`I`, `they`), auxiliaries (`is`, `have`), prepositions, possessives (`'s`), title abbreviations (`Dr.`, `Mr.`), or inside common multi-word phrases (`going to`, `a lot of`, `in order to`) is heavily penalized.
* **Early-cue punctuation protection:** Starting a new cue mid-thought when a comma or clause break appears within the first 3 words incurs a penalty so those trailing words stay attached to the previous cue.
* **Reading speed & duration:** Cues faster than the target reading speed (`15 CPS`), shorter than `1,000 ms`, longer than `7,000 ms`, or consisting of tiny 1–2 word orphans receive escalating penalties.
* **Line-break symmetry:** When wrapping a cue onto two lines, the scorer balances line lengths while applying similar grammar penalties so function words are not stranded at the end of line 1.
* **Local VTT cadence tie-breaker:** After optimal boundaries are chosen, unpunctuated splits can shift by up to 3 words if nearby inline WebVTT timestamps show a clear local speech pause—provided the new split does not violate any grammar or readability rule.


### Timing preservation

Inline VTT timestamps provide anchors within source cues. Words between anchors still need estimated timing, and the final spoken word or chunk may need a bounded tail estimate. Timing preservation does not mean every output timestamp is copied unchanged from YouTube.

### Speaker markers

YouTube's `>>` marker forces a new rendered subtitle line and becomes `- ` in the final output. It does not identify the speaker or independently force a new timed cue.

### Non-speech annotations

Descriptive bracketed text is removed, including supported annotations spanning nearby source cues. Boundaries left by removed annotation-only cues are retained to avoid joining speech across them.

The detection uses bracket structure and alphabetic content, not a fixed dictionary of sound labels. Literal bracketed dialogue can therefore be removed too.

</details>

<details>
<summary><h2>Default settings</h2></summary>

| Setting | Default |
|---|---|
| Characters per line | `42` |
| Lines per cue | `2` |
| Target reading speed | `15 CPS` |
| Maximum reading-speed target | `22 CPS` |
| Maximum word-rate target | `400 WPM` |
| Minimum cue-duration target | `1,000 ms` |
| Maximum cue-duration target | `7,000 ms` |
| Minimum cue gap | `24 ms` |
| Post-speech hold | `500 ms` |
| Spoken-cue chaining threshold | `500 ms` |
| Readability padding | `250 ms` |
| Normal speech-region gap threshold | `650 ms` |
| Maximum eligible sentence-tail bridge gap | `1,250 ms` |
| VTT cadence lookaround | `3 words` |
| Terminal VTT tail estimate bounds | `250–1,000 ms` |

CPS means characters per second. WPM means words per minute.

These values are constants near the beginning of `Clean-Auto-Subs.py`. The plugin does not provide a separate configuration file or custom command-line settings for them.

Readability values are processing targets, not a guarantee that every final cue meets them. Source timing and available gaps can limit the result.

### Subtitle readability defaults are based on public subtitling guidance from WCAG and Samtext, as well as subtitling standards set by Netflix and the BBC.

</details>

<details>
<summary><h2>Logs and results</h2></summary>

CleanAutoSubs reports through yt-dlp's normal output. In MeTube, inspect the container logs.

The summary includes:

- Plugin, grammar, and timing-layer versions
- Source, de-rolled, dialogue-source, re-segmented, and final cue counts
- Repetition removal, annotation removal, and fragment-repair counts
- Timing mode and available VTT anchors
- Speaker-marker conversions
- Structural checks for empty cues, invalid durations, overlaps, and remaining markers
- Readability counts for short, fast, or oversized cues

The supplied version identifies its grammar layer as `RG1.2-opt` and its timing layer as `VTT1.6`.

A readability advisory means one or more targets remain unmet. It does not by itself mean subtitle processing failed. Structural warnings deserve a review of the generated file; the current implementation writes the output before reporting those warnings.

</details>

<details>
<summary><h2>Troubleshooting</h2></summary>

### Plugin is not found

Check the package layout, filename, and installation environment. Use `CleanAutoSubs` with matching capitalization. Add `--verbose` to inspect loading, and confirm plugin loading has not been disabled.

### No subtitles to clean

Confirm automatic captions exist for the requested language and that subtitle downloading is enabled. Run `yt-dlp --list-subs "VIDEO_URL"` to inspect the available tracks.

### Creator or non-English subtitles are unchanged

This is expected for linguistic cleanup. Only selected English automatic-caption tracks are cleaned; conversion to SRT can still occur.

### Timing source says legacy interpolation

Request VTT input and ensure no earlier postprocessor converts it before CleanAutoSubs runs. Some source tracks lack usable inline timestamps, and captured timing can also be rejected by the sanity check.

### Embedded subtitles still look unprocessed

Confirm cleanup runs before embedding. Use the `before_dl` setting shown in the examples and check that your player is displaying the intended track.

### FFmpeg conversion fails

Confirm FFmpeg is available inside the environment running yt-dlp. For MeTube, that means inside the container. Review the preceding FFmpeg error for the specific cause.

### Readability warnings remain

Fast speech, long words, or tight source timing can prevent every target from being met. Review the affected captions alongside the audio before changing the profile constants.

</details>

<details>
<summary><h2>Reference and limitations</h2></summary>

- Public beta behavior should be reviewed on representative videos.
- English grammar rules are heuristic and will not fit every sentence.
- The cleaner uses downloaded caption text and timing; it does not analyze audio.
- Incorrect words and missing punctuation in the source are not automatically corrected.
- Translated English automatic captions may be selected by a broad language pattern.
- VTT anchors do not provide exact start and end times for every word.
- Readability and structural checks report results without guaranteeing an error-free track.
- Descriptive sound annotations are intentionally removed, so the result is a dialogue-focused subtitle track rather than a full preservation of accessibility captions.
- There is no standalone SRT-file command-line interface in this module.
- Compatibility depends on the yt-dlp and FFmpeg versions used by the host application.

</details>
