# CleanAutoSubs

**Version 3.2.0-beta.9 — Public beta**

A yt-dlp postprocessor that cleans up English YouTube automatic captions, removes rolling repetition, rebuilds subtitle cues, and improves line breaks using source WebVTT timing where available.

> CleanAutoSubs works with captions YouTube has already generated. It does not transcribe audio, translate dialogue, or correct recognition mistakes.

## Highlights

- Removes repeated text from rolling automatic captions
- Rebuilds cues around sentences, phrases, and speech regions
- Uses English grammar heuristics to improve cue and line breaks
- Captures inline WebVTT timestamps before converting subtitles to SRT
- Uses local source timing to refine suitable cue boundaries
- Removes descriptive bracketed annotations such as `[Music]`
- Preserves spoken-word censorship placeholders such as `[ __ ]`
- Converts YouTube `>>` speaker markers into readable dash dialogue
- Reports timing, cleanup, and readability results
- Requires no external language model or NLP service

## Processing model

- Linguistic cleanup applies only to selected English automatic-caption tracks.
- Creator-authored and non-English tracks are skipped by the cleaner, but may still be converted to SRT by the shared conversion step.
- Source timing takes priority when forcing a readability target would distort timing.
- If captured WebVTT timing is unavailable or fails the sanity check, processing falls back to timing derived from the converted SRT.
- The completed SRT is written to a temporary file before replacing the working subtitle file.
- Final structural checks report warnings; they do not block writing the output.

> The working SRT is replaced during cleanup. CleanAutoSubs does not create a separate backup of it, and yt-dlp controls whether intermediate subtitle files are retained.

## Requirements

- A working yt-dlp installation
- FFmpeg available to yt-dlp for subtitle conversion and optional embedding
- English automatic captions available for the selected video
- Write access to the subtitle output directory

Use a Python version supported by your installed yt-dlp release when running yt-dlp through Python. See the [yt-dlp requirements](https://github.com/yt-dlp/yt-dlp#dependencies).

## Project files

| File | Purpose |
|---|---|
| `Clean-Auto-Subs.py` | Plugin implementation; provides `CleanAutoSubsPP` |
| `README.md` | Setup, usage, and reference documentation |

The plugin name used in configuration is `CleanAutoSubs`, without the `PP` suffix.

## Quick start

1. Install the plugin using the folder layout below.
2. Confirm yt-dlp and FFmpeg work in the same environment.
3. Run this command, replacing `VIDEO_URL` with a YouTube video URL:

```sh
yt-dlp --write-auto-subs --sub-langs "en.*" --sub-format "vtt" --use-postprocessor "CleanAutoSubs:when=before_dl" "VIDEO_URL"
```

This downloads the video and requests English automatic captions in WebVTT format. The plugin converts the selected subtitles to SRT and cleans supported tracks.

The example uses `before_dl` so cleanup runs after subtitle files are written and before video downloading and later embedding. This ordering follows [yt-dlp's download pipeline](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py).

Check the output for a `CleanAutoSubs` version summary and a `Timing source:` line. Play the result and review the captions before processing a larger batch.

<details>
<summary><h2>Plugin installation</h2></summary>

Place `clean_autosubs.py` inside this package structure:

```text
cleanautosubs/
└── yt_dlp_plugins/
    └── postprocessor/
        └── clean_autosubs.py
```

Example Windows location:

```text
%APPDATA%\yt-dlp\plugins\cleanautosubs\yt_dlp_plugins\postprocessor\clean_autosubs.py
```

Example Linux system location, also suitable for a container mount:

```text
/etc/yt-dlp/plugins/cleanautosubs/yt_dlp_plugins/postprocessor/clean_autosubs.py
```

These follow the [yt-dlp plugin installation layout](https://github.com/yt-dlp/yt-dlp#installing-plugins). Run a download with `--verbose` to inspect plugin loading.

Running `python clean_autosubs.py` does not process a subtitle file. This module is loaded and called by yt-dlp.

</details>

<details>
<summary><h2>Usage options</h2></summary>

### Download and embed cleaned subtitles

```sh
yt-dlp --write-auto-subs --sub-langs "en.*" --sub-format "vtt" --use-postprocessor "CleanAutoSubs:when=before_dl" --embed-subs --merge-output-format mkv --remux-video mkv "VIDEO_URL"
```

This requests an MKV output with embedded subtitles. Review the selected subtitle track in your player.

### Download only the subtitles

```sh
yt-dlp --skip-download --write-auto-subs --sub-langs "en.*" --sub-format "vtt" --use-postprocessor "CleanAutoSubs:when=before_dl" "VIDEO_URL"
```

### Prefer creator subtitles when available

Add `--write-subs` alongside `--write-auto-subs`. For a selected language with creator subtitles, yt-dlp can select those instead. CleanAutoSubs skips their linguistic cleanup.

### Select a specific English track

Use `--list-subs "VIDEO_URL"` to inspect available language codes, then replace `en.*` with the desired English code. The broader expression can select multiple English variants or translated tracks.

See the [yt-dlp option reference](https://github.com/yt-dlp/yt-dlp#usage-and-options) for subtitle selection and embedding options.

</details>

<details>
<summary><h2>MeTube configuration</h2></summary>

Mount the plugin package into the container at the Linux path shown above. Keep the file on persistent host storage so container replacement does not remove it.

Merge these settings into your existing `YTDL_OPTIONS` JSON:

```json
{
  "writeautomaticsub": true,
  "subtitleslangs": ["en.*"],
  "subtitlesformat": "vtt",
  "postprocessors": [
    {
      "key": "CleanAutoSubs",
      "when": "before_dl"
    },
    {
      "key": "FFmpegEmbedSubtitle"
    }
  ]
}
```

Preserve other settings and postprocessors you already use. Choose a subtitle-capable output container, such as MKV, when embedding. Omit the `FFmpegEmbedSubtitle` entry if you only want separate subtitle files.

Restart the container after installing or replacing the plugin. Run one download and check the container log for the CleanAutoSubs summary.

MeTube passes API-style options through `YTDL_OPTIONS`; see its [configuration reference](https://github.com/alexta69/metube#yt-dlp-options). Container paths and existing postprocessor settings depend on your installation.

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

These values are constants near the beginning of `clean_autosubs.py`. The plugin does not provide a separate configuration file or custom command-line settings for them.

Readability values are processing targets, not a guarantee that every final cue meets them. Source timing and available gaps can limit the result.

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
