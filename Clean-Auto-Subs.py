import html
import math
import os
import re

from yt_dlp.postprocessor import FFmpegSubtitlesConvertorPP
from yt_dlp.postprocessor.common import PostProcessor


# CleanAutoSubs public beta: deterministic cleanup and re-segmentation of
# YouTube automatic captions with source WebVTT timing preservation.
PLUGIN_VERSION = "3.2.0-beta.9"
TIMING_LAYER_VERSION = "VTT1.6"

# ---------------------------------------------------------------------------
# Subtitle readability profile
# ---------------------------------------------------------------------------

SINGLE_LINE_MAX = 42
OPTIMAL_CPS = 15
MAX_CPS = 22
MAX_WPM = 400
MIN_DURATION_MS = 1000
MAX_DURATION_MS = 7000
MIN_GAP_MS = 24
POST_SPEECH_HOLD_MS = 500
MAX_CHAIN_GAP_MS = 500
READABILITY_PADDING_MS = 250
MAX_LINES = 2
UNBREAK_SHORTER_THAN = 33

# Nearby source cues are treated as one stretch of continuous speech and
# re-segmented from scratch. A larger silence is preserved as a hard break.
REGION_GAP_MS = 650

# A gap above REGION_GAP_MS normally creates a hard speech-region boundary.
# Exception: if the previous cue is unfinished and the first few words of the
# next cue complete that sentence, permit the existing DP segmenter to consider
# both sides together. This does not force the cues to merge.
REGION_TAIL_BRIDGE_MAX_GAP_MS = 1250
REGION_TAIL_BRIDGE_MAX_WORDS = 3

# Final DP cue-start repair. If a proposed cue begins while the preceding cue is
# still unfinished and reaches medium punctuation (, ; :) within its first few
# spoken words, penalize that awkward start. The normal +55 comma boundary plus
# this 125-point start penalty creates the same 180-point preference swing as a
# sentence-ending boundary, without globally promoting every comma in the track.
EARLY_CUE_PUNCTUATION_MAX_WORDS = 3
EARLY_CUE_MEDIUM_START_PENALTY = 125

# Lowest-priority cadence refinement. After the normal DP has chosen cue boundaries,
# an unpunctuated boundary may move by at most this many words when exact source-VTT
# cadence supplies a stronger local break. Punctuation/structure remain authoritative;
# weak grammar may be reconsidered, while new boundaries pass a strict no-break gate.
# No millisecond threshold is invented: only relative timing in this window matters.
VTT_CADENCE_LOOKAROUND_WORDS = 3

# Maximum cue text capacity implied by the configured line-length/line-count limits.
MAX_CUE_CHARS = SINGLE_LINE_MAX * MAX_LINES

# YouTube uses >> as a visual speaker-turn marker in some automatic captions.
# It is a layout boundary only: it forces a new subtitle line, but it does NOT
# create a new timed cue or speech region. It stays as >> internally so later
# repair/reflow passes preserve the structure, then becomes "- " at the very
# end for conventional readable dialogue formatting.
SPEAKER_MARKER = ">>"
SPEAKER_INTERNAL_PREFIX = ">> "
SPEAKER_DISPLAY_PREFIX = "- "

_TIME_RE = re.compile(
    r'(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*'
    r'(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})'
)
_STRONG_END_RE = re.compile(r'[.!?]["\')\]]*$')
_MEDIUM_END_RE = re.compile(r'[,;:]["\')\]]*$')
_SOUND_EFFECT_RE = re.compile(r'^\[[^\]]+\]$')
_CENSOR_MARKER_CONTENT_RE = re.compile(r'^\s*_+\s*$')

# Non-speech annotations are stripped from the dialogue track after de-rolling.
# YouTube's spoken-word censorship placeholder (normally ``[ __ ]``) is explicitly
# protected because it stands in for dialogue rather than describing a sound.
NON_SPEECH_MAX_CROSS_CUE_SPAN = 3

# WebVTT accepts [hh:]mm:ss.mmm timestamps. YouTube automatic captions can
# also place these timestamps inline in the cue payload before individual
# words/chunks; CleanAutoSubs captures those anchors before converting to SRT.
_VTT_TIMESTAMP = r'(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}'
_VTT_CUE_TIME_RE = re.compile(
    rf'(?P<start>{_VTT_TIMESTAMP})\s*-->\s*(?P<end>{_VTT_TIMESTAMP})'
)
_VTT_INLINE_TIME_RE = re.compile(rf'<(?P<time>{_VTT_TIMESTAMP})>')
_VTT_TAG_RE = re.compile(r'<[^>]*>')

# YouTube can keep the final word of a rolling-caption window displayed for
# several seconds after speech has actually ended. The final inline VTT timestamp
# gives that word/chunk's start but not always its exact end, so VTT1.3/VTT1.4/VTT1.5
# estimate only this otherwise-unknown terminal tail within conservative bounds.
VTT_TERMINAL_HOLD_MIN_MS = 250
VTT_TERMINAL_HOLD_MAX_MS = 1000

# A period after these tokens normally belongs to the abbreviation rather than
# ending a sentence. Keeping this definition near _ends_strongly() makes the
# exception apply consistently throughout line scoring and cue scoring.
_TITLE_ABBREVIATIONS = {
    'mr.', 'mrs.', 'ms.', 'dr.', 'prof.', 'sr.', 'jr.',
}

# Heuristic phrase-boundary vocabulary. This is intentionally lightweight:
# no external NLP model is required inside the MeTube container.
_CONJUNCTIONS = {
    'and', 'but', 'or', 'so', 'yet', 'nor', 'for',
    'because', 'although', 'though', 'while', 'whereas',
    'if', 'when', 'where', 'unless', 'until', 'since',
}
_PREPOSITIONS = {
    'about', 'above', 'across', 'after', 'against', 'along', 'among', 'around',
    'at', 'before', 'behind', 'below', 'beneath', 'beside', 'between', 'beyond',
    'by', 'despite', 'during', 'except', 'for', 'from', 'in', 'inside', 'into',
    'near', 'of', 'off', 'on', 'onto', 'out', 'outside', 'over', 'past',
    'through', 'to', 'toward', 'under', 'until', 'up', 'upon', 'with', 'within',
    'without',
}
_DETERMINERS = {
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    'my', 'your', 'his', 'her', 'its', 'our', 'their',
}
_SUBJECT_PRONOUNS = {'i', 'you', 'he', 'she', 'it', 'we', 'they'}
_AUXILIARIES = {
    'am', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'do', 'does', 'did',
    'have', 'has', 'had',
    'can', 'could', 'will', 'would', 'shall', 'should',
    'may', 'might', 'must',
}


def _to_ms(value):
    value = value.replace('.', ',')
    hours, minutes, rest = value.split(':')
    seconds, millis = rest.split(',')
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int(millis)
    )


def _format_ms(value):
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}'


def _vtt_to_ms(value):
    parts = value.split(':')
    if len(parts) == 2:
        hours = 0
        minutes, rest = parts
    elif len(parts) == 3:
        hours, minutes, rest = parts
    else:
        raise ValueError(f'Invalid WebVTT timestamp: {value!r}')

    seconds, millis = rest.split('.')
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int(millis)
    )


def _is_english_language(language):
    language = (language or '').lower()
    return language == 'en' or language.startswith(('en-', 'en_'))


def _is_title_abbreviation(token):
    return token.strip().lower().replace('’', "'") in _TITLE_ABBREVIATIONS


def _normalize_text(text):
    # Keep speaker markers tokenizable even if the source has odd spacing such
    # as "text>>next" or "text >>next". They remain structural markers until
    # the final rendering pass.
    text = re.sub(r'\s*>>\s*', ' >> ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _cue_text(cue):
    return _normalize_text(' '.join(cue['lines']))


def _plain_word(token):
    return re.sub(r'^[^\w]+|[^\w]+$', '', token).lower()


def _word_count(text):
    return len(re.findall(r"\b[\w’'-]+\b", text))


def _duration_ms(cue):
    return max(1, cue['end'] - cue['start'])


def _cps(text, duration_ms):
    return len(text) / max(duration_ms / 1000.0, 0.001)


def _wpm(text, duration_ms):
    return _word_count(text) / max(duration_ms / 60000.0, 0.001)


def _minimum_readable_duration_ms(text):
    """Return the shortest display time that satisfies hard readability limits."""
    char_target = math.ceil((len(text) / MAX_CPS) * 1000) if text else 0
    word_target = math.ceil((_word_count(text) / MAX_WPM) * 60000) if text else 0
    return min(
        MAX_DURATION_MS,
        max(MIN_DURATION_MS, char_target, word_target),
    )


def _ends_strongly(text):
    stripped = text.strip()
    if _is_title_abbreviation(stripped):
        return False
    return bool(_STRONG_END_RE.search(stripped))


def _ends_medium(text):
    return bool(_MEDIUM_END_RE.search(text.strip()))


def _is_censor_marker_content(content):
    """Return True for YouTube's bracketed spoken-word censorship placeholder."""
    return bool(_CENSOR_MARKER_CONTENT_RE.fullmatch(content or ''))


def _is_non_speech_annotation_content(content):
    """Return True for descriptive bracket content such as music or screaming."""
    if _is_censor_marker_content(content):
        return False
    return any(character.isalpha() for character in (content or ''))


def _is_non_speech_annotation_text(text):
    """Return True when text is exactly one removable bracketed annotation."""
    text = _normalize_text(text)
    while text.startswith(SPEAKER_MARKER):
        text = text[len(SPEAKER_MARKER):].lstrip()

    if not _SOUND_EFFECT_RE.fullmatch(text):
        return False
    return _is_non_speech_annotation_content(text[1:-1])


def _cross_cue_annotation_can_close(cues, cue_index, text):
    """Conservatively allow a bracket annotation to span only a few source cues."""
    open_index = text.rfind('[')
    if open_index < 0 or ']' in text[open_index + 1:]:
        return False

    content_parts = [text[open_index + 1:]]
    for offset in range(1, NON_SPEECH_MAX_CROSS_CUE_SPAN):
        future_index = cue_index + offset
        if future_index >= len(cues):
            break

        future_text = _cue_text(cues[future_index])
        close_index = future_text.find(']')
        if close_index >= 0:
            content_parts.append(future_text[:close_index])
            return _is_non_speech_annotation_content(' '.join(content_parts))

        content_parts.append(future_text)

    return False


def _strip_non_speech_text(text, annotation_open=False, allow_cross_cue_open=False):
    """
    Remove descriptive bracket annotations while preserving censorship markers.

    annotation_open carries a confirmed multi-cue annotation across source cues.
    An unmatched ``[`` is only allowed to open such a span when a nearby closing
    bracket was found in advance, preventing malformed text from swallowing the
    remainder of a subtitle track.
    """
    text = _normalize_text(text)
    output = []
    annotations_removed = 0
    censor_markers_preserved = 0
    index = 0

    while index < len(text):
        if annotation_open:
            close_index = text.find(']', index)
            if close_index < 0:
                # The caller confirmed a nearby closing bracket before opening
                # this span, so this source cue belongs entirely to the annotation.
                return (
                    _normalize_text(''.join(output)),
                    True,
                    annotations_removed,
                    censor_markers_preserved,
                )
            index = close_index + 1
            annotation_open = False
            continue

        if text[index] != '[':
            output.append(text[index])
            index += 1
            continue

        close_index = text.find(']', index + 1)
        if close_index >= 0:
            content = text[index + 1:close_index]
            bracketed = text[index:close_index + 1]

            if _is_censor_marker_content(content):
                output.append(bracketed)
                censor_markers_preserved += 1
            elif _is_non_speech_annotation_content(content):
                annotations_removed += 1
            else:
                # Unknown non-descriptive bracket syntax is safer to retain.
                output.append(bracketed)

            index = close_index + 1
            continue

        # Unmatched opening bracket. Only consume it as a cross-cue annotation
        # when the caller has already verified a nearby closing bracket.
        content = text[index + 1:]
        if allow_cross_cue_open and _is_non_speech_annotation_content(content):
            annotations_removed += 1
            annotation_open = True
            break

        output.append(text[index:])
        break

    cleaned = _normalize_text(''.join(output))

    # If stripping removed the only text after a speaker marker, do not leave a
    # structural ``>>`` orphan that could later become a lone dash subtitle.
    words = cleaned.split()
    while words and words[-1] == SPEAKER_MARKER:
        words.pop()
    if words and all(word == SPEAKER_MARKER for word in words):
        words = []
    cleaned = ' '.join(words)

    return cleaned, annotation_open, annotations_removed, censor_markers_preserved


def _retime_after_annotation_strip(cue, original_text, cleaned_text):
    """Keep source VTT timing when the surviving words remain an exact subsequence."""
    timed_tokens = cue.get('timed_tokens') or []
    if not timed_tokens:
        return False

    original_words = original_text.split()
    timed_words = [token['text'] for token in timed_tokens]
    cleaned_words = cleaned_text.split()

    if timed_words != original_words:
        cue.pop('timed_tokens', None)
        cue.pop('line_token_counts', None)
        return True

    kept = []
    source_index = 0
    for cleaned_word in cleaned_words:
        while source_index < len(original_words) and original_words[source_index] != cleaned_word:
            source_index += 1
        if source_index >= len(original_words):
            cue.pop('timed_tokens', None)
            cue.pop('line_token_counts', None)
            return True
        kept.append(dict(timed_tokens[source_index]))
        source_index += 1

    if len(kept) != len(cleaned_words):
        cue.pop('timed_tokens', None)
        cue.pop('line_token_counts', None)
        return True

    cue['timed_tokens'] = kept
    cue['line_token_counts'] = [len(kept)]
    if kept:
        cue['start'] = max(int(cue['start']), int(kept[0]['start']))
    return False


def _should_bridge_region_gap(previous_cue, next_cue, gap_ms):
    """
    Return True when a normal region gap appears to sever a sentence or
    continuation phrase.

    Bridging only keeps both source cues in the same speech region; the
    existing DP segmenter still decides where the final cue break belongs.

    A bridge is allowed when:
      * the gap is no larger than REGION_TAIL_BRIDGE_MAX_GAP_MS, and
      * the previous cue is not already a completed sentence, and
      * either:
          - the previous cue ends in medium punctuation (, ; :), or
          - the first few words of the next cue complete the sentence.
    """
    if gap_ms > REGION_TAIL_BRIDGE_MAX_GAP_MS:
        return False

    previous_text = _cue_text(previous_cue)
    next_text = _cue_text(next_cue)

    if not previous_text or not next_text:
        return False

    # A completed previous sentence has no tail to rescue.
    if _ends_strongly(previous_text):
        return False

    next_words = next_text.split()
    if not next_words:
        return False

    # Do not bridge across explicit speaker-turn structure or a leading
    # standalone sound-effect token.
    if next_words[0] == SPEAKER_MARKER or _SOUND_EFFECT_RE.match(next_words[0]):
        return False

    # Medium punctuation explicitly indicates that the thought continues.
    # Across a short source pause, keep both sides in the same speech region
    # and let the existing DP decide where the final cue boundary belongs.
    if _ends_medium(previous_text):
        return True

    # Local continuation check, including medium punctuation:
    # if the first few words of the next cue reach either a sentence ending
    # or explicit continuation punctuation, allow the DP to inspect both
    # sides together.
    limit = min(REGION_TAIL_BRIDGE_MAX_WORDS, len(next_words))

    for count in range(1, limit + 1):
        leading_words = next_words[:count]

        if SPEAKER_MARKER in leading_words:
            return False

        leading_text = ' '.join(leading_words)

        if _ends_strongly(leading_text) or _ends_medium(leading_text):
            return True

    return False


def _split_speaker_segments(text):
    """
    Split text on explicit YouTube >> markers without assigning speaker IDs.

    A marker means only: "the following text must begin on a new rendered
    subtitle line." It does not imply a new cue, new timing region, or inferred
    speaker identity.
    """
    text = _normalize_text(text)
    if not text:
        return []

    if SPEAKER_MARKER not in text:
        return [(False, text)]

    pieces = text.split(SPEAKER_MARKER)
    segments = []

    # Text before the first marker is ordinary continuation text, if present.
    leading = pieces[0].strip()
    if leading:
        segments.append((False, leading))

    # Every non-empty piece after a marker is an explicit speaker-style turn.
    for piece in pieces[1:]:
        piece = piece.strip()
        if piece:
            segments.append((True, piece))

    return segments


def _render_speaker_markers(cues):
    """
    Convert internal line-leading >> markers to conventional "- " dialogue.

    This happens only after all repair/timing/reflow passes so the structural
    marker cannot be lost by a later cue merge.
    """
    rendered = []
    converted = 0

    for cue in cues:
        new_cue = {
            'start': cue['start'],
            'end': cue['end'],
            'lines': [],
        }

        for line in cue['lines']:
            stripped = line.strip()
            if stripped.startswith(SPEAKER_MARKER):
                body = stripped[len(SPEAKER_MARKER):].lstrip()
                new_cue['lines'].append(SPEAKER_DISPLAY_PREFIX + body)
                converted += 1
            else:
                new_cue['lines'].append(stripped)

        rendered.append(new_cue)

    return rendered, converted



def _strip_vtt_markup(text):
    """Return visible WebVTT payload text while discarding timing/style tags."""
    text = _VTT_INLINE_TIME_RE.sub('', text)
    text = _VTT_TAG_RE.sub('', text)
    text = html.unescape(text)
    text = text.replace('\u200e', '').replace('\u200f', '').replace('\ufeff', '')
    return _normalize_text(text)


def _distribute_tokens_in_interval(
    words,
    start,
    end,
    inline_anchored,
    vtt_end_bounded,
):
    """Assign token intervals inside one VTT-timestamped text chunk."""
    if not words:
        return []

    start = int(start)
    end = max(start + 1, int(end))
    duration = end - start
    total_units = sum(max(1, len(word)) for word in words) + max(0, len(words) - 1)

    result = []
    position = 0
    for index, word in enumerate(words):
        word_start = start + round(duration * position / total_units)
        position += max(1, len(word))
        word_end = start + round(duration * position / total_units)
        result.append({
            'text': word,
            'start': word_start,
            'end': max(word_start + 1, word_end),
            'vtt_inline': bool(inline_anchored),
            'vtt_timed': True,
            'vtt_end_bounded': bool(vtt_end_bounded),
            # Only the first token after a literal inline <timestamp> owns an
            # exact source onset. Later words in the same timestamped chunk are
            # interpolated and must not masquerade as VTT truth for cadence.
            'vtt_exact_start': bool(inline_anchored and index == 0),
        })
        if index < len(words) - 1:
            position += 1

    # Keep the final token inside the source interval even after rounding.
    result[-1]['end'] = min(max(result[-1]['start'] + 1, result[-1]['end']), end)
    return result


def _recent_bounded_vtt_token_ms(cues):
    """Estimate local word timing from the most recent source-bounded VTT tokens."""
    durations = []

    for cue in reversed(cues):
        for token in reversed(cue.get('timed_tokens') or []):
            if not token.get('vtt_timed') or not token.get('vtt_end_bounded'):
                continue

            duration = int(token['end']) - int(token['start'])
            if 20 <= duration <= VTT_TERMINAL_HOLD_MAX_MS:
                durations.append(duration)
                if len(durations) >= 3:
                    break
        if len(durations) >= 3:
            break

    if not durations:
        return VTT_TERMINAL_HOLD_MIN_MS

    ordered = sorted(durations)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        estimate = ordered[middle]
    else:
        estimate = round((ordered[middle - 1] + ordered[middle]) / 2)

    return max(
        VTT_TERMINAL_HOLD_MIN_MS,
        min(VTT_TERMINAL_HOLD_MAX_MS, estimate),
    )


def _extract_vtt_timed_tokens(raw_payload, cue_start, cue_end):
    """
    Extract YouTube WebVTT inline timing anchors before FFmpeg flattens them.

    An inline <hh:mm:ss.mmm> tag marks the start of the following text chunk.
    If a chunk contains multiple whitespace-delimited tokens, timing inside that
    smaller source interval is still interpolated by token length. This keeps
    the old deterministic fallback while using much tighter source anchors.
    """
    matches = list(_VTT_INLINE_TIME_RE.finditer(raw_payload))
    if not matches:
        return [], 0

    segments = []

    # Text before the first inline timestamp starts at the cue's own source time.
    # YouTube rolling captions commonly use two payload lines here: the first is
    # old context carried forward from the previous cue, while the second begins
    # with the first genuinely new word. Treat those source lines independently
    # so the old line cannot consume most of the cue_start -> first-anchor interval
    # and make the first new word appear artificially late after de-rolling.
    prefix_raw = raw_payload[:matches[0].start()]
    prefix_lines = prefix_raw.split('\n')
    for prefix_line in prefix_lines:
        if not _strip_vtt_markup(prefix_line):
            continue
        segments.append({
            'start': cue_start,
            'raw': prefix_line,
            'inline': False,
        })

    for index, match in enumerate(matches):
        next_pos = matches[index + 1].start() if index + 1 < len(matches) else len(raw_payload)
        try:
            anchor = _vtt_to_ms(match.group('time'))
        except ValueError:
            continue
        segments.append({
            'start': max(cue_start, min(cue_end, anchor)),
            'raw': raw_payload[match.end():next_pos],
            'inline': True,
        })

    timed_tokens = []
    inline_anchor_count = len(matches)

    for index, segment in enumerate(segments):
        visible = _strip_vtt_markup(segment['raw'])
        words = visible.split()
        if not words:
            continue

        next_start = cue_end
        vtt_end_bounded = False
        for future in segments[index + 1:]:
            if future['start'] >= segment['start']:
                next_start = future['start']
                vtt_end_bounded = True
                break

        interval_end = max(segment['start'] + 1, min(cue_end, next_start))
        timed_tokens.extend(_distribute_tokens_in_interval(
            words,
            segment['start'],
            interval_end,
            inline_anchored=segment['inline'],
            vtt_end_bounded=vtt_end_bounded,
        ))

    return timed_tokens, inline_anchor_count


def _estimate_vtt_terminal_tail_ms(tokens, terminal_index=None):
    """Estimate an unbounded final VTT token tail from nearby bounded timing."""
    if not tokens:
        return VTT_TERMINAL_HOLD_MIN_MS

    if terminal_index is None:
        terminal_index = len(tokens) - 1

    terminal = tokens[terminal_index]
    source_cue_start = terminal.get('vtt_cue_start')
    recent_durations = []

    for token in tokens[:terminal_index]:
        if not token.get('vtt_timed') or not token.get('vtt_end_bounded'):
            continue
        if (
            source_cue_start is not None
            and token.get('vtt_cue_start') != source_cue_start
        ):
            continue

        duration = int(token['end']) - int(token['start'])
        if 20 <= duration <= VTT_TERMINAL_HOLD_MAX_MS:
            recent_durations.append(duration)

    recent_durations = recent_durations[-3:]
    if recent_durations:
        ordered = sorted(recent_durations)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            estimated_tail = ordered[middle]
        else:
            estimated_tail = round((ordered[middle - 1] + ordered[middle]) / 2)
    else:
        estimated_tail = VTT_TERMINAL_HOLD_MIN_MS

    return max(
        VTT_TERMINAL_HOLD_MIN_MS,
        min(VTT_TERMINAL_HOLD_MAX_MS, estimated_tail),
    )


def _best_terminal_speech_end(tokens, end_index):
    """
    Return the best-known speech end for a newly segmented cue's final token.

    A VTT token whose interval is bounded by a following inline anchor keeps its
    existing end. For an unbounded terminal inline token, a hold of one second or
    less is accepted as plausible; a longer hold is treated as rolling-caption
    presentation time and replaced by the same conservative tail estimate used by
    VTT1.3. Legacy/interpolated tokens retain their existing behavior.
    """
    terminal = tokens[end_index]
    terminal_start = int(terminal['start'])
    terminal_end = max(terminal_start + 1, int(terminal['end']))

    if terminal.get('vtt_source_start_only'):
        estimated_end = int(
            terminal.get('vtt_source_start_estimated_end', terminal_end)
        )
        estimated_end = max(terminal_start + 1, min(terminal_end, estimated_end))
        return estimated_end, 'vtt_source_start_estimate'

    if not terminal.get('vtt_timed'):
        return terminal_end, 'legacy'

    if terminal.get('vtt_end_bounded'):
        return terminal_end, 'vtt_bounded'

    if not terminal.get('vtt_inline'):
        return terminal_end, 'vtt_unbounded_source'

    raw_hold = terminal_end - terminal_start
    if raw_hold <= VTT_TERMINAL_HOLD_MAX_MS:
        return terminal_end, 'vtt_terminal_source'

    estimated_tail = _estimate_vtt_terminal_tail_ms(tokens, end_index)
    speech_end = min(terminal_end, terminal_start + estimated_tail)
    return max(terminal_start + 1, speech_end), 'vtt_terminal_estimate'


def _trim_terminal_vtt_hold_before_reset(cue):
    """
    Shorten a stale YouTube rolling-window hold immediately before a reset.

    The last inline WebVTT timestamp tells us when the final word/chunk starts,
    but at a fresh caption-window boundary the cue end can be many seconds later
    because YouTube keeps the old rolling text visible through intervening silence.
    Treat that cue end as a presentation-window boundary, not a spoken-word end.

    The final word's exact end is not present in WebVTT, so estimate only that
    missing tail from the median of up to three recent *bounded* token durations
    in the same cue. Keep the estimate between the existing readability padding
    and one second. This correction is deliberately limited to cues immediately
    before an explicit whitespace-signaled caption-window reset.
    """
    timed_tokens = cue.get('timed_tokens') or []
    if not timed_tokens:
        return False

    terminal = timed_tokens[-1]
    if not terminal.get('vtt_inline'):
        return False

    terminal_start = int(terminal['start'])
    terminal_end = int(terminal['end'])
    raw_hold = terminal_end - terminal_start

    # Normal rolling cues commonly end a few hundred milliseconds after their
    # last inline anchor. Only intervene when the source window is clearly
    # holding that final word beyond our one-second uncertainty ceiling.
    if raw_hold <= VTT_TERMINAL_HOLD_MAX_MS:
        return False

    recent_durations = []
    for token in timed_tokens[:-1]:
        duration = int(token['end']) - int(token['start'])
        # Ignore the 1 ms placeholder intervals produced by stale carried-forward
        # lines; retain only plausible source-bounded word/chunk intervals.
        if 20 <= duration <= VTT_TERMINAL_HOLD_MAX_MS:
            recent_durations.append(duration)

    recent_durations = recent_durations[-3:]
    if recent_durations:
        ordered = sorted(recent_durations)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            estimated_tail = ordered[middle]
        else:
            estimated_tail = round((ordered[middle - 1] + ordered[middle]) / 2)
    else:
        estimated_tail = VTT_TERMINAL_HOLD_MIN_MS

    estimated_tail = max(
        VTT_TERMINAL_HOLD_MIN_MS,
        min(VTT_TERMINAL_HOLD_MAX_MS, estimated_tail),
    )
    new_end = min(terminal_end, terminal_start + estimated_tail)

    if new_end >= terminal_end:
        return False

    terminal['end'] = max(terminal_start + 1, new_end)
    terminal['vtt_terminal_estimated'] = True
    return True


def _parse_vtt_with_timing(data):
    """
    Parse YouTube WebVTT before conversion and preserve usable inline timing.

    Returned cues use the same {start, end, lines} shape as SRT cues. Cues with
    valid inline timing additionally carry timed_tokens and line_token_counts.
    """
    data = data.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')
    lines = data.splitlines()
    cues = []
    inline_anchor_count = 0
    timed_cue_count = 0
    timed_token_count = 0
    terminal_holds_trimmed = 0

    index = 0
    while index < len(lines):
        timing_match = _VTT_CUE_TIME_RE.search(lines[index])
        if not timing_match:
            index += 1
            continue

        try:
            cue_start = _vtt_to_ms(timing_match.group('start'))
            cue_end = _vtt_to_ms(timing_match.group('end'))
        except ValueError:
            index += 1
            continue

        cue_end = max(cue_start + 1, cue_end)
        index += 1
        payload_lines = []
        vtt_window_reset = False

        # YouTube sometimes starts a fresh rolling-caption window with one or
        # more whitespace-only payload lines before the first real text line.
        # A simple ``line.strip()`` loop mistakes that whitespace for the blank
        # line that terminates a WebVTT cue and silently drops the real caption.
        #
        # Skip leading whitespace inside the cue, but stop once real payload has
        # started or another timing line begins. A non-empty whitespace-only line
        # is also preserved as a structural "fresh caption window" signal so the
        # region builder will not rejoin text across that reset later.
        while index < len(lines):
            raw_line = lines[index]

            # Do not consume the next cue's timing line when the current source
            # cue contains only whitespace (YouTube emits tiny reset cues too).
            if _VTT_CUE_TIME_RE.search(raw_line):
                break

            if not raw_line.strip():
                if payload_lines:
                    break

                if raw_line != '':
                    vtt_window_reset = True

                index += 1
                continue

            payload_lines.append(raw_line)
            index += 1

        visible_lines = [
            visible
            for raw_line in payload_lines
            if (visible := _strip_vtt_markup(raw_line))
        ]
        if not visible_lines:
            continue

        cue = {
            'start': cue_start,
            'end': cue_end,
            'lines': visible_lines,
        }
        if vtt_window_reset:
            cue['vtt_window_reset'] = True

        raw_payload = '\n'.join(payload_lines)
        timed_tokens, anchor_count = _extract_vtt_timed_tokens(
            raw_payload,
            cue_start,
            cue_end,
        )
        for token in timed_tokens:
            token['vtt_cue_start'] = cue_start
            token['vtt_cue_end'] = cue_end
        inline_anchor_count += anchor_count

        # VTT1.5: a no-inline-anchor rolling update still provides one useful
        # timing fact: every payload line is visible when the source cue begins.
        # Do not feed an estimate into DP segmentation here; that would change cue
        # selection globally. Instead preserve only a local cadence estimate so
        # de-rolling can later recognize genuinely new text and carry a separate
        # speech-end estimate alongside the unchanged legacy segmentation timing.
        if not anchor_count:
            cue['vtt_no_anchor_word_ms'] = _recent_bounded_vtt_token_ms(cues)

        # Safety check: only attach token timing when tokenization exactly matches
        # the visible cue text. A mismatch falls back to deterministic timing
        # interpolation rather than risking text/timing misalignment.
        visible_tokens = _cue_text(cue).split()
        timed_text = [token['text'] for token in timed_tokens]
        if anchor_count and timed_tokens and timed_text == visible_tokens:
            cue['timed_tokens'] = timed_tokens
            cue['line_token_counts'] = [
                len(_normalize_text(line).split()) for line in visible_lines
            ]
            timed_cue_count += 1
            timed_token_count += len(timed_tokens)

        # A whitespace-prefixed fresh window is also reliable evidence that the
        # preceding rolling cue's long tail was presentation hold, not speech.
        # Correct only that immediately preceding terminal token; all ordinary
        # ordinary continuous-speech VTT timing remains untouched.
        if vtt_window_reset and cues:
            if _trim_terminal_vtt_hold_before_reset(cues[-1]):
                terminal_holds_trimmed += 1

        cues.append(cue)

    return cues, {
        'source_cues': len(cues),
        'timed_cues': timed_cue_count,
        'timed_tokens': timed_token_count,
        'inline_anchors': inline_anchor_count,
        'window_resets': sum(1 for cue in cues if cue.get('vtt_window_reset')),
        'terminal_holds_trimmed': terminal_holds_trimmed,
    }


def _vtt_timing_sanity(srt_cues, vtt_cues):
    """Return whether captured VTT timing is close enough to the converted SRT."""
    cue_delta = abs(len(vtt_cues) - len(srt_cues))
    cue_tolerance = max(8, round(len(srt_cues) * 0.10))
    srt_words = sum(_word_count(_cue_text(cue)) for cue in srt_cues)
    vtt_words = sum(_word_count(_cue_text(cue)) for cue in vtt_cues)
    word_ratio = (vtt_words / srt_words) if srt_words else 1.0
    is_sane = cue_delta <= cue_tolerance and 0.90 <= word_ratio <= 1.10
    return is_sane, cue_delta, word_ratio


def _read_source_vtt(selected):
    """Read the selected VTT before FFmpegSubtitlesConvertorPP can flatten it."""
    if str((selected or {}).get('ext') or '').lower() != 'vtt':
        return None

    data = selected.get('data')
    if isinstance(data, bytes):
        data = data.decode('utf-8-sig', errors='replace')
    if isinstance(data, str) and data.lstrip('\ufeff \t\r\n').startswith('WEBVTT'):
        return data

    filepath = selected.get('filepath')
    if filepath and os.path.isfile(filepath):
        with open(filepath, 'r', encoding='utf-8-sig') as handle:
            return handle.read()

    return None


def _capture_vtt_timing_sources(requested_subtitles, auto_languages):
    """Capture English automatic-caption VTT timing before SRT conversion."""
    captured = {}
    errors = []

    for language, selected in (requested_subtitles or {}).items():
        if language not in auto_languages or not _is_english_language(language):
            continue

        try:
            data = _read_source_vtt(selected)
            if not data:
                continue

            cues, stats = _parse_vtt_with_timing(data)
            if cues and stats['timed_cues']:
                captured[language] = {
                    'cues': cues,
                    'stats': stats,
                }
        except Exception as error:
            # VTT timing is an enhancement, never a reason to lose the proven
            # SRT fallback or fail an otherwise valid download.
            errors.append((language, f'{type(error).__name__}: {error}'))

    return captured, errors

def _parse_srt(data):
    data = data.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not data:
        return []

    cues = []
    for block in re.split(r'\n\s*\n', data):
        lines = block.splitlines()

        timing_index = None
        timing_match = None
        for index, line in enumerate(lines):
            match = _TIME_RE.search(line)
            if match:
                timing_index = index
                timing_match = match
                break

        if timing_index is None:
            continue

        text_lines = [
            line.strip()
            for line in lines[timing_index + 1:]
            if line.strip()
        ]
        if not text_lines:
            continue

        cues.append({
            'start': _to_ms(timing_match.group('start')),
            'end': _to_ms(timing_match.group('end')),
            'lines': text_lines,
        })

    return cues


# ---------------------------------------------------------------------------
# Pass 1: remove YouTube's rolling caption behavior
# ---------------------------------------------------------------------------

def _clean_rolling_cues(cues):
    cleaned = []
    removed_rolling_lines = 0
    fixed_overlaps = 0
    duplicate_merges = 0

    for cue in cues:
        current = {
            'start': cue['start'],
            'end': cue['end'],
            'lines': list(cue['lines']),
        }
        if cue.get('vtt_window_reset'):
            current['vtt_window_reset'] = True
        if cue.get('timed_tokens'):
            current['timed_tokens'] = [dict(token) for token in cue['timed_tokens']]
            current['line_token_counts'] = list(cue.get('line_token_counts') or [])
        if cue.get('vtt_no_anchor_word_ms') is not None:
            current['vtt_no_anchor_word_ms'] = int(cue['vtt_no_anchor_word_ms'])

        if cleaned:
            previous = cleaned[-1]

            max_overlap = min(len(previous['lines']), len(current['lines']))
            overlap = 0
            for size in range(max_overlap, 0, -1):
                if previous['lines'][-size:] == current['lines'][:size]:
                    overlap = size
                    break

            if overlap:
                if current.get('timed_tokens') and current.get('line_token_counts'):
                    dropped_tokens = sum(current['line_token_counts'][:overlap])
                    current['timed_tokens'] = current['timed_tokens'][dropped_tokens:]
                    current['line_token_counts'] = current['line_token_counts'][overlap:]
                    if current['timed_tokens']:
                        # The VTT cue may have started early only because it was
                        # carrying old rolling text. Move the surviving cue start
                        # to the first remaining source-timed token when possible.
                        current['start'] = max(
                            current['start'],
                            current['timed_tokens'][0]['start'],
                        )

                current['lines'] = current['lines'][overlap:]
                removed_rolling_lines += overlap

                # VTT1.5: when a no-anchor source cue loses carried rolling lines
                # and still has text left, that surviving text is genuinely new at
                # this cue's source start. Preserve that fact separately from the
                # legacy token intervals used by the DP segmenter.
                if current['lines'] and current.get('vtt_no_anchor_word_ms') is not None:
                    current['vtt_source_start_only'] = True

                # This cue was only a rolling duplicate. Keep the previous text
                # and preserve the later end time.
                if not current['lines']:
                    previous['end'] = max(previous['end'], current['end'])
                    duplicate_merges += 1
                    continue

            # Source auto-captions frequently overlap. Remove simultaneous cue
            # activity before the re-segmentation pass.
            if previous['end'] >= current['start']:
                previous['end'] = max(
                    previous['start'] + 1,
                    current['start'] - 1,
                )
                fixed_overlaps += 1

        if current['end'] <= current['start']:
            current['end'] = current['start'] + 1

        cleaned.append(current)

    return cleaned, removed_rolling_lines, fixed_overlaps, duplicate_merges


# ---------------------------------------------------------------------------
# Pass 1.5: remove non-speech bracket annotations from the dialogue track
# ---------------------------------------------------------------------------

def _strip_non_speech_annotations(cues):
    """
    Strip YouTube sound-description annotations without discarding their structure.

    Inline annotations are removed from the text. Annotation-only source cues are
    omitted and become hard boundaries so later region bridging, fragment repair,
    and sub-500 ms display chaining cannot silently join dialogue across the
    removed event. The spoken-word censorship marker ``[ __ ]`` is preserved.
    """
    result = []
    annotation_open = False
    pending_boundary = False
    stats = {
        'annotations_removed': 0,
        'annotation_only_cues_removed': 0,
        'cross_cue_spans': 0,
        'hard_boundaries_preserved': 0,
        'censor_markers_preserved': 0,
        'vtt_timing_fallbacks': 0,
    }

    for cue_index, cue in enumerate(cues):
        original_text = _cue_text(cue)
        was_open = annotation_open
        allow_cross_cue_open = (
            not annotation_open
            and _cross_cue_annotation_can_close(cues, cue_index, original_text)
        )

        cleaned_text, annotation_open, removed, protected = _strip_non_speech_text(
            original_text,
            annotation_open=annotation_open,
            allow_cross_cue_open=allow_cross_cue_open,
        )

        stats['annotations_removed'] += removed
        stats['censor_markers_preserved'] += protected
        if not was_open and annotation_open:
            stats['cross_cue_spans'] += 1

        new_cue = dict(cue)
        if cue.get('timed_tokens'):
            new_cue['timed_tokens'] = [dict(token) for token in cue['timed_tokens']]
        if cue.get('line_token_counts'):
            new_cue['line_token_counts'] = list(cue['line_token_counts'])

        changed_text = cleaned_text != original_text
        if cleaned_text:
            new_cue['lines'] = [cleaned_text]

            if changed_text and new_cue.get('timed_tokens'):
                if _retime_after_annotation_strip(new_cue, original_text, cleaned_text):
                    stats['vtt_timing_fallbacks'] += 1

            boundary_before = pending_boundary or was_open
            boundary_after = annotation_open

            if boundary_before:
                new_cue['non_speech_boundary_before'] = True
            if boundary_after:
                new_cue['non_speech_boundary_after'] = True

            result.append(new_cue)
            pending_boundary = False
            continue

        # An annotation-only cue disappears visually, but it must remain a hard
        # structural separator between any spoken material on either side.
        if removed or was_open:
            stats['annotation_only_cues_removed'] += 1
            if result:
                result[-1]['non_speech_boundary_after'] = True
            pending_boundary = True
            continue

        # Defensive fallback: preserve any unexpectedly empty non-annotation cue.
        new_cue['lines'] = list(cue.get('lines') or [])
        result.append(new_cue)

    # Count unique preserved transitions rather than the two flags around them.
    stats['hard_boundaries_preserved'] = sum(
        1 for cue in result if cue.get('non_speech_boundary_before')
    )

    return result, stats


# ---------------------------------------------------------------------------
# Pass 2: reconstruct approximate word timing
# ---------------------------------------------------------------------------

def _tokenize_cue_with_times(cue):
    """
    Give each word an approximate position inside its source cue.

    Preferred path: preserve YouTube WebVTT inline timing captured before SRT
    conversion. Fallback path: deterministic interpolation by token length.
    """
    timed_tokens = cue.get('timed_tokens') or []
    if timed_tokens:
        # Copy so downstream segmentation cannot mutate the source cue cache.
        result = [dict(token) for token in timed_tokens]

        # Respect the cleaned cue boundaries as hard safety rails. Pass 1 may
        # shorten an overlapping source cue or advance its start after removing
        # rolling text. Clamping here preserves the useful VTT anchors without
        # allowing stale rolling-window timing to make tokens overlap.
        cue_start = int(cue['start'])
        cue_end = max(cue_start + 1, int(cue['end']))
        previous_end = cue_start

        for token in result:
            raw_start = int(token['start'])
            raw_end = int(token['end'])
            token['start'] = max(previous_end, cue_start, min(raw_start, cue_end - 1))
            token['end'] = max(
                token['start'] + 1,
                min(max(raw_end, token['start'] + 1), cue_end),
            )
            previous_end = token['end']

        return result

    text = _cue_text(cue)
    words = text.split()
    if not words:
        return []

    start = cue['start']
    end = max(start + 1, cue['end'])
    duration = end - start

    # Legacy deterministic fallback. Token length plus inter-word spaces is a
    # better approximation than giving "a" and "extraordinary" equal shares.
    total_units = sum(max(1, len(word)) for word in words) + max(0, len(words) - 1)

    tokens = []
    position = 0
    for index, word in enumerate(words):
        word_start = start + round(duration * position / total_units)
        position += max(1, len(word))
        word_end = start + round(duration * position / total_units)

        tokens.append({
            'text': word,
            'start': word_start,
            'end': max(word_start + 1, word_end),
            'vtt_inline': False,
            'vtt_timed': False,
            'vtt_end_bounded': False,
        })

        if index < len(words) - 1:
            position += 1

    if cue.get('vtt_source_start_only'):
        # Preserve DP behavior: token start/end above remain the same deterministic
        # legacy interpolation used before VTT1.5. The additional estimate is used
        # only later as speech-end provenance for display timing.
        word_ms = max(
            VTT_TERMINAL_HOLD_MIN_MS,
            min(
                VTT_TERMINAL_HOLD_MAX_MS,
                int(cue.get('vtt_no_anchor_word_ms', VTT_TERMINAL_HOLD_MIN_MS)),
            ),
        )
        estimated_duration = min(end - start, max(1, word_ms * len(words)))
        estimated_units = sum(max(1, len(word)) for word in words) + max(0, len(words) - 1)
        estimated_position = 0

        for index, token in enumerate(tokens):
            estimated_position += max(1, len(words[index]))
            estimated_end = start + round(
                estimated_duration * estimated_position / estimated_units
            )
            token['vtt_source_start_only'] = True
            token['vtt_source_start_estimated_end'] = max(
                start + 1,
                min(end, estimated_end),
            )
            if index < len(words) - 1:
                estimated_position += 1

    return tokens


def _build_speech_regions(cues):
    """
    Group nearby source cues into continuous speech regions.

    Within a region, the original YouTube cue boundaries are deliberately
    discarded. A silence longer than REGION_GAP_MS normally remains a hard
    boundary, except for a short sentence-tail bridge that lets the existing
    DP segmenter inspect both sides before deciding where to break.
    """
    regions = []
    current = []

    for cue in cues:
        if current:
            # A whitespace-prefixed YouTube VTT cue marks a fresh rolling-caption
            # window. Preserve that source reset as a hard region boundary even
            # when the timestamps themselves are close together; bridging it back
            # together would undo the structural information we just recovered.
            if (
                cue.get('vtt_window_reset')
                or cue.get('non_speech_boundary_before')
                or current[-1].get('non_speech_boundary_after')
            ):
                regions.append(current)
                current = []
            else:
                gap_ms = cue['start'] - current[-1]['end']

                if gap_ms > REGION_GAP_MS and not _should_bridge_region_gap(
                    current[-1],
                    cue,
                    gap_ms,
                ):
                    regions.append(current)
                    current = []

        current.append(cue)

    if current:
        regions.append(current)

    return regions


# ---------------------------------------------------------------------------
# Linguistic line breaking
# ---------------------------------------------------------------------------

def _line_break_score(
    words,
    split_index,
    left_limit=SINGLE_LINE_MAX,
    right_limit=SINGLE_LINE_MAX,
):
    left_words = words[:split_index]
    right_words = words[split_index:]

    if not left_words or not right_words:
        return float('-inf')

    left = ' '.join(left_words)
    right = ' '.join(right_words)

    if len(left) > left_limit or len(right) > right_limit:
        return float('-inf')

    previous = left_words[-1]
    next_word = right_words[0]
    previous_plain = _plain_word(previous)
    next_plain = _plain_word(next_word)

    # Balance matters, but grammar can outweigh perfect visual symmetry.
    score = -abs(len(left) - len(right)) * 1.0

    if _ends_strongly(previous):
        score += 50
    elif _ends_medium(previous):
        score += 18

    if next_plain in _CONJUNCTIONS:
        score += 16
    elif next_plain in _PREPOSITIONS:
        score += 8

    # Avoid separating grammatical units that normally belong together.
    if previous_plain in _DETERMINERS:
        score -= 70
    if previous_plain in _SUBJECT_PRONOUNS:
        score -= 55
    if previous_plain in _AUXILIARIES:
        score -= 60
    if previous_plain in _PREPOSITIONS and not _ends_medium(previous):
        score -= 35
    if previous.lower().endswith(("'s", "’s")):
        score -= 80

    return score


def _wrap_single_segment(text, prefix=''):
    """
    Wrap one structural text segment to one or two lines.

    prefix is used for an internal line-leading speaker marker. Continuation
    lines do not repeat the marker.
    """
    text = _normalize_text(text)
    if not text:
        return None

    first_limit = SINGLE_LINE_MAX - len(prefix)
    if first_limit <= 0:
        return None

    if len(text) <= UNBREAK_SHORTER_THAN and len(prefix) + len(text) <= SINGLE_LINE_MAX:
        return [prefix + text]

    if len(prefix) + len(text) <= SINGLE_LINE_MAX:
        return [prefix + text]

    words = text.split()
    best_lines = None
    best_score = float('-inf')

    for split_index in range(1, len(words)):
        score = _line_break_score(
            words,
            split_index,
            left_limit=first_limit,
            right_limit=SINGLE_LINE_MAX,
        )
        if score > best_score:
            left = ' '.join(words[:split_index])
            right = ' '.join(words[split_index:])
            if (
                len(prefix) + len(left) <= SINGLE_LINE_MAX
                and len(right) <= SINGLE_LINE_MAX
            ):
                best_score = score
                best_lines = [prefix + left, right]

    return best_lines


def _wrap_linguistically(text):
    """
    Linguistically wrap a cue while respecting explicit >> line boundaries.

    Rules:
      * >> always begins a new subtitle line.
      * No line may contain two >> markers.
      * >> does NOT force a new timed cue.
      * One marked turn may still use a continuation second line.
      * Two structural turns may share one cue only when each fits on one line.
      * More than two structural turns cannot fit a two-line cue, so the normal
        re-segmentation search must choose a cue boundary between them.
    """
    text = _normalize_text(text)
    if not text:
        return None

    segments = _split_speaker_segments(text)
    if not segments:
        return None

    # Ordinary wrapping behavior applies when there is no speaker marker.
    if len(segments) == 1 and not segments[0][0]:
        return _wrap_single_segment(segments[0][1])

    # A single explicit turn can use both lines. The marker appears only on the
    # first line and is converted to "- " in the final rendering pass.
    if len(segments) == 1 and segments[0][0]:
        return _wrap_single_segment(
            segments[0][1],
            prefix=SPEAKER_INTERNAL_PREFIX,
        )

    # With two structural segments, each gets exactly one line so a marker
    # never appears mid-line and one speaker-style turn cannot run into another.
    if len(segments) == 2:
        lines = []
        for marked, segment_text in segments:
            prefix = SPEAKER_INTERNAL_PREFIX if marked else ''
            rendered = prefix + segment_text
            if len(rendered) > SINGLE_LINE_MAX:
                return None
            lines.append(rendered)
        return lines

    # Three or more structural turns cannot be represented within MAX_LINES=2.
    # Returning None makes the existing dynamic segmentation find a cue break;
    # it does not invent new timing merely because a marker exists.
    return None


# ---------------------------------------------------------------------------
# Linguistic cue boundary scoring + dynamic re-segmentation
# ---------------------------------------------------------------------------

def _cue_boundary_score(tokens, end_index):
    """
    Score a boundary after tokens[end_index]. Higher means a more natural stop.
    """
    token = tokens[end_index]['text']
    token_plain = _plain_word(token)
    next_plain = (
        _plain_word(tokens[end_index + 1]['text'])
        if end_index + 1 < len(tokens)
        else ''
    )

    score = 0

    # Sentence boundaries dominate everything else.
    if _ends_strongly(token):
        score += 180
    elif _ends_medium(token):
        score += 55

    # Clause starts can be good boundaries, but "and" is deliberately only a
    # modest hint because it can also join a noun phrase ("Ken and Roberta").
    if next_plain in _CONJUNCTIONS:
        score += 14
    elif next_plain in _PREPOSITIONS:
        score += 7

    # Strong penalties for ugly grammatical splits.
    if token_plain in _DETERMINERS:
        score -= 100
    if token_plain in _SUBJECT_PRONOUNS:
        score -= 75
    if token_plain in _AUXILIARIES:
        score -= 80
    if token_plain in _PREPOSITIONS and not _ends_medium(token):
        score -= 55
    if token.lower().endswith(("'s", "’s")):
        score -= 110

    return score

# ---------------------------------------------------------------------------
# Rich grammar layer RG1.1 (optimized)
#
# Adds local grammar-aware boundary scoring without rebuilding or normalizing
# an entire speech region for every dynamic-programming candidate.
# ---------------------------------------------------------------------------

GRAMMAR_LAYER_VERSION = "RG1.2-opt"

_RG_CONTRACTIONS = {
    "i'm": ('i', 'am'),
    "you're": ('you', 'are'),
    "he's": ('he', 'is', 'has'),
    "she's": ('she', 'is', 'has'),
    "it's": ('it', 'is', 'has'),
    "we're": ('we', 'are'),
    "they're": ('they', 'are'),
    "i've": ('i', 'have'),
    "you've": ('you', 'have'),
    "we've": ('we', 'have'),
    "they've": ('they', 'have'),
    "i'll": ('i', 'will'),
    "you'll": ('you', 'will'),
    "he'll": ('he', 'will'),
    "she'll": ('she', 'will'),
    "it'll": ('it', 'will'),
    "we'll": ('we', 'will'),
    "they'll": ('they', 'will'),
    "i'd": ('i', 'would', 'had'),
    "you'd": ('you', 'would', 'had'),
    "he'd": ('he', 'would', 'had'),
    "she'd": ('she', 'would', 'had'),
    "we'd": ('we', 'would', 'had'),
    "they'd": ('they', 'would', 'had'),
    "don't": ('do', 'not'),
    "doesn't": ('does', 'not'),
    "didn't": ('did', 'not'),
    "isn't": ('is', 'not'),
    "aren't": ('are', 'not'),
    "wasn't": ('was', 'not'),
    "weren't": ('were', 'not'),
    "haven't": ('have', 'not'),
    "hasn't": ('has', 'not'),
    "hadn't": ('had', 'not'),
    "can't": ('can', 'not'),
    "couldn't": ('could', 'not'),
    "won't": ('will', 'not'),
    "wouldn't": ('would', 'not'),
    "shouldn't": ('should', 'not'),
    "mustn't": ('must', 'not'),
    "mightn't": ('might', 'not'),
    "needn't": ('need', 'not'),
    "that's": ('that', 'is', 'has'),
    "there's": ('there', 'is', 'has'),
    "here's": ('here', 'is'),
    "what's": ('what', 'is', 'has'),
    "who's": ('who', 'is', 'has'),
    "let's": ('let', 'us'),
}

_RG_AUXILIARY_FORMS = {
    'am', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'do', 'does', 'did', 'have', 'has', 'had',
    'can', 'could', 'will', 'would', 'shall', 'should',
    'may', 'might', 'must', 'need',
}

_RG_SUBJECT_FORMS = {
    'i', 'you', 'he', 'she', 'it', 'we', 'they',
    'who', 'whoever', 'what', 'whatever',
}

_RG_EXTRA_DETERMINERS = {
    'each', 'every', 'either', 'neither', 'some', 'any', 'no',
    'enough', 'much', 'many', 'few', 'several', 'all', 'both',
    'half', 'such', 'what', 'whatever', 'which', 'whichever', 'whose',
}

_RG_PREDICATE_LINKERS = {
    'gonna', 'wanna', 'gotta', 'hafta',
    'very', 'quite', 'extremely', 'really',
}

_RG_CLAUSE_STARTERS = {
    'although', 'though', 'because', 'while', 'whereas', 'unless',
    'until', 'since', 'once', 'whenever', 'wherever', 'whether',
    'if', 'when', 'where',
}

_RG_TRANSITIONS = {
    'however', 'therefore', 'meanwhile', 'instead', 'otherwise',
    'anyway', 'anyhow', 'still', 'then', 'also', 'besides',
    'moreover', 'furthermore', 'consequently', 'nevertheless',
    'nonetheless', 'finally',
}

_RG_TITLE_ABBREVIATIONS = _TITLE_ABBREVIATIONS

_RG_NO_BREAK_PHRASES = {
    ('a', 'lot', 'of'),
    ('a', 'bunch', 'of'),
    ('a', 'couple', 'of'),
    ('a', 'few', 'of'),
    ('kind', 'of'),
    ('sort', 'of'),
    ('type', 'of'),
    ('one', 'of'),
    ('some', 'of'),
    ('all', 'of'),
    ('both', 'of'),
    ('each', 'of'),
    ('none', 'of'),
    ('most', 'of'),
    ('part', 'of'),
    ('because', 'of'),
    ('instead', 'of'),
    ('out', 'of'),
    ('due', 'to'),
    ('according', 'to'),
    ('thanks', 'to'),
    ('prior', 'to'),
    ('in', 'order', 'to'),
    ('so', 'as', 'to'),
    ('as', 'well', 'as'),
    ('as', 'long', 'as'),
    ('as', 'soon', 'as'),
    ('as', 'far', 'as'),
    ('as', 'much', 'as'),
    ('as', 'many', 'as'),
    ('rather', 'than'),
    ('more', 'than'),
    ('less', 'than'),
    ('other', 'than'),
    ('at', 'least'),
    ('at', 'most'),
    ('at', 'all'),
    ('such', 'as'),
    ('even', 'though'),
    ('even', 'if'),
    ('as', 'if'),
    ('what', 'if'),
    ('whether', 'or', 'not'),
    ('not', 'only'),
    ('but', 'also'),
    ('have', 'to'),
    ('has', 'to'),
    ('had', 'to'),
    ('having', 'to'),
    ('need', 'to'),
    ('needs', 'to'),
    ('needed', 'to'),
    ('want', 'to'),
    ('wants', 'to'),
    ('wanted', 'to'),
    ('try', 'to'),
    ('tries', 'to'),
    ('trying', 'to'),
    ('used', 'to'),
    ('supposed', 'to'),
    ('able', 'to'),
    ('going', 'to'),
    ('would', 'have'),
    ('could', 'have'),
    ('should', 'have'),
    ('might', 'have'),
    ('must', 'have'),
    ('will', 'have'),
    ('have', 'been'),
    ('has', 'been'),
    ('had', 'been'),
    ('will', 'be'),
    ('would', 'be'),
    ('could', 'be'),
    ('should', 'be'),
    ('might', 'be'),
    ('must', 'be'),
}

# Index phrases by length once at import time. Membership tests against these
# sets are O(1), and the longest phrase above is only a few tokens long.
_RG_NO_BREAK_BY_LENGTH = {}
for _rg_phrase in _RG_NO_BREAK_PHRASES:
    _RG_NO_BREAK_BY_LENGTH.setdefault(len(_rg_phrase), set()).add(_rg_phrase)
del _rg_phrase


def _rg_normalize_token(token):
    return _plain_word(token).replace('’', "'").lower()


def _rg_text_at(sequence, index, token_dicts=False):
    item = sequence[index]
    return item['text'] if token_dicts else item


def _rg_crosses_fixed_phrase_local(sequence, split_index, token_dicts=False, cache=None):
    """
    Return True when the proposed boundary falls inside a known fixed phrase.

    Only the tiny window around split_index is inspected. This is the key
    performance difference from RG1, which normalized the entire speech region
    for every candidate boundary tested by the dynamic-programming segmenter.
    """
    if split_index <= 0 or split_index >= len(sequence):
        return False

    if cache is None:
        cache = {}

    def normalized_at(index):
        if index not in cache:
            cache[index] = _rg_normalize_token(
                _rg_text_at(sequence, index, token_dicts=token_dicts)
            )
        return cache[index]

    sequence_len = len(sequence)

    for size, phrases in _RG_NO_BREAK_BY_LENGTH.items():
        # A phrase of `size` crosses split_index only if it starts within these
        # local positions. No other part of the speech region can matter.
        first_start = max(0, split_index - size + 1)
        last_start = min(split_index - 1, sequence_len - size)

        for start in range(first_start, last_start + 1):
            candidate = tuple(normalized_at(i) for i in range(start, start + size))
            if candidate in phrases:
                return True

    return False


def _rg_adjustment_local(sequence, split_index, mode='line', token_dicts=False):
    """
    Return an additive grammar score using only the boundary neighborhood.

    Positive values make a break more attractive; negative values discourage
    it. The weights intentionally match RG1 so this optimization changes the
    cost of computing the grammar signal, not the intended grammar behavior.
    """
    if split_index <= 0 or split_index >= len(sequence):
        return 0.0

    cache = {}

    def raw_at(index):
        return _rg_text_at(sequence, index, token_dicts=token_dicts)

    def normalized_at(index):
        if index not in cache:
            cache[index] = _rg_normalize_token(raw_at(index))
        return cache[index]

    previous_raw = raw_at(split_index - 1)
    next_raw = raw_at(split_index)

    # Speaker markers remain exclusively under the base structural handling.
    if previous_raw == SPEAKER_MARKER or next_raw == SPEAKER_MARKER:
        return 0.0

    previous = normalized_at(split_index - 1)
    next_word = normalized_at(split_index)
    previous_forms = set(_RG_CONTRACTIONS.get(previous, (previous,))) if previous else set()

    if mode == 'cue':
        phrase_penalty = -190
        abbreviation_penalty = -260
        auxiliary_penalty = -58
        subject_penalty = -48
        determiner_penalty = -42
        linker_penalty = -58
        infinitive_penalty = -45
        transition_bonus = 24
        clause_bonus = 14
    else:
        phrase_penalty = -120
        abbreviation_penalty = -130
        auxiliary_penalty = -38
        subject_penalty = -30
        determiner_penalty = -28
        linker_penalty = -40
        infinitive_penalty = -30
        transition_bonus = 15
        clause_bonus = 9

    score = 0.0

    # Preserve punctuation on this specific test: _plain_word intentionally
    # strips it, but the period is what distinguishes "Dr." from ordinary "dr".
    if previous_raw.lower().replace('’', "'") in _RG_TITLE_ABBREVIATIONS:
        score += abbreviation_penalty

    if _rg_crosses_fixed_phrase_local(
        sequence,
        split_index,
        token_dicts=token_dicts,
        cache=cache,
    ):
        score += phrase_penalty

    if previous_forms & _RG_AUXILIARY_FORMS:
        score += auxiliary_penalty
    if previous_forms & _RG_SUBJECT_FORMS:
        score += subject_penalty

    if previous in _RG_EXTRA_DETERMINERS:
        score += determiner_penalty

    if previous in _RG_PREDICATE_LINKERS:
        score += linker_penalty

    if previous == 'to':
        score += infinitive_penalty

    if next_word in _RG_TRANSITIONS:
        score += transition_bonus
        if _ends_strongly(previous_raw):
            score += transition_bonus
        elif _ends_medium(previous_raw):
            score += transition_bonus * 0.5
    elif next_word in _RG_CLAUSE_STARTERS:
        score += clause_bonus

    return score


# Save the base scorers, then wrap them with RG1.1 local adjustments.
# Timing, de-rolling, speaker markers, cue repair, and rendering stay separate.
_rg_base_line_break_score = _line_break_score
_rg_base_cue_boundary_score = _cue_boundary_score


def _line_break_score(
    words,
    split_index,
    left_limit=SINGLE_LINE_MAX,
    right_limit=SINGLE_LINE_MAX,
):
    base = _rg_base_line_break_score(
        words,
        split_index,
        left_limit=left_limit,
        right_limit=right_limit,
    )
    if base == float('-inf'):
        return base
    return base + _rg_adjustment_local(
        words,
        split_index,
        mode='line',
        token_dicts=False,
    )


def _cue_boundary_score(tokens, end_index):
    base = _rg_base_cue_boundary_score(tokens, end_index)
    return base + _rg_adjustment_local(
        tokens,
        end_index + 1,
        mode='cue',
        token_dicts=True,
    )


def _early_cue_medium_start_penalty(tokens, start_index):
    """
    Penalize a cue start that strands nearby continuation punctuation.

    If the preceding cue ends without strong sentence punctuation and the new
    cue would encounter a comma, semicolon, or colon within its first few spoken
    words, starting here is probably less natural than carrying those few words
    backward and breaking at that punctuation. Completed sentences, speaker
    turns, and leading sound-description structure are left alone.
    """
    if start_index <= 0 or start_index >= len(tokens):
        return 0

    previous_text = tokens[start_index - 1]['text']
    if _ends_strongly(previous_text):
        return 0

    first_text = tokens[start_index]['text']
    if first_text == SPEAKER_MARKER or _SOUND_EFFECT_RE.match(first_text):
        return 0

    words_seen = 0
    for index in range(start_index, len(tokens)):
        token_text = tokens[index]['text']

        if token_text == SPEAKER_MARKER or _SOUND_EFFECT_RE.match(token_text):
            return 0

        words_seen += _word_count(token_text)
        if words_seen > EARLY_CUE_PUNCTUATION_MAX_WORDS:
            break

        # A nearby sentence ending already has strong native boundary pressure.
        # This rule exists specifically to give early medium punctuation parity.
        if _ends_strongly(token_text):
            return 0

        if _ends_medium(token_text):
            return EARLY_CUE_MEDIUM_START_PENALTY

    return 0


def _cadence_boundary_is_locked(tokens, end_index):
    """
    Return True when an existing boundary is owned by higher-priority structure.

    Cadence remains the last-ranked signal. Explicit punctuation, speaker structure,
    and EarlyPunctStart3 are authoritative. Weak grammar preferences (for example,
    a modest bonus before "and" or a clause starter) do not lock the DP boundary;
    they may be reconsidered when nearby exact VTT cadence gives better evidence.
    """
    if end_index < 0 or end_index + 1 >= len(tokens):
        return True

    previous_text = tokens[end_index]['text']
    next_text = tokens[end_index + 1]['text']

    if previous_text == SPEAKER_MARKER or next_text == SPEAKER_MARKER:
        return True
    if _ends_strongly(previous_text) or _ends_medium(previous_text):
        return True
    if _early_cue_medium_start_penalty(tokens, end_index + 1):
        return True

    return False


def _cadence_candidate_is_acceptable(tokens, end_index):
    """
    Return True when Cadence may CREATE a boundary after tokens[end_index].

    This gate is deliberately stricter than the gate for reconsidering an existing
    DP boundary. Cadence may move away from a weak or awkward grammatical choice,
    but it may not create a known no-break split, strand a function word, or create
    the same early-punctuation problem that EarlyPunctStart3 was added to prevent.
    """
    if end_index < 0 or end_index + 1 >= len(tokens):
        return False

    previous_text = tokens[end_index]['text']
    next_text = tokens[end_index + 1]['text']

    if previous_text == SPEAKER_MARKER or next_text == SPEAKER_MARKER:
        return False

    # A punctuation boundary is a perfectly valid landing point. Cadence is still
    # not allowed to cross another punctuation boundary to get there.
    if _ends_strongly(previous_text) or _ends_medium(previous_text):
        return True

    if _early_cue_medium_start_penalty(tokens, end_index + 1):
        return False

    # Never invent a split inside an RG fixed/no-break phrase.
    if _rg_crosses_fixed_phrase_local(
        tokens,
        end_index + 1,
        token_dicts=True,
    ):
        return False

    previous_plain = _plain_word(previous_text)

    # Avoid leaving a syntactically dependent word hanging at the end of a cue.
    # Conjunctions are included here even though starting a cue with one can be a
    # weakly acceptable DP preference: "forever and | ever" is worse than moving
    # away from "forever | and ever".
    if previous_plain in _CONJUNCTIONS:
        return False
    if previous_plain in _DETERMINERS:
        return False
    if previous_plain in _SUBJECT_PRONOUNS:
        return False
    if previous_plain in _AUXILIARIES:
        return False
    if previous_plain in _PREPOSITIONS and not _ends_medium(previous_text):
        return False
    if previous_text.lower().endswith(("'s", "’s")):
        return False

    previous_rg = _rg_normalize_token(previous_text)
    previous_forms = set(_RG_CONTRACTIONS.get(previous_rg, (previous_rg,))) if previous_rg else set()

    if previous_text.lower().replace('’', "'") in _RG_TITLE_ABBREVIATIONS:
        return False
    if previous_forms & _RG_AUXILIARY_FORMS:
        return False
    if previous_forms & _RG_SUBJECT_FORMS:
        return False
    if previous_rg in _RG_EXTRA_DETERMINERS:
        return False
    if previous_rg in _RG_PREDICATE_LINKERS:
        return False
    if previous_rg == 'to':
        return False

    return True


def _exact_vtt_onset_interval(tokens, end_index):
    """Return exact source-VTT onset spacing across a boundary, or None."""
    if end_index < 0 or end_index + 1 >= len(tokens):
        return None

    previous = tokens[end_index]
    following = tokens[end_index + 1]
    if not previous.get('vtt_exact_start') or not following.get('vtt_exact_start'):
        return None

    interval = int(following['start']) - int(previous['start'])
    return interval if interval > 0 else None


def _cadence_quality_flags(tokens, start_index, end_index):
    """Return existing readability-threshold violations for one candidate span."""
    candidate = _candidate_cost(tokens, start_index, end_index)
    if candidate is None:
        return None

    _, lines, text, start, end = candidate
    duration = max(1, end - start)

    # Match the terminal-DP guard when the region's final token has a stale
    # presentation hold; this is scoring/validation only, never a timing rewrite.
    scoring_duration = duration
    if duration > MAX_DURATION_MS + 500 and end_index == len(tokens) - 1:
        speech_end, speech_end_source = _best_terminal_speech_end(tokens, end_index)
        if speech_end_source in {'vtt_terminal_estimate', 'vtt_source_start_estimate'}:
            scoring_duration = max(1, int(speech_end) - int(start))

    return (
        int(scoring_duration < MIN_DURATION_MS),
        int(scoring_duration > MAX_DURATION_MS),
        int(_cps(text, scoring_duration) > MAX_CPS),
        int(_wpm(text, scoring_duration) > MAX_WPM),
        int(len(lines) > MAX_LINES),
        int(any(len(line) > SINGLE_LINE_MAX for line in lines)),
    )


def _cadence_can_reach(tokens, current_end, candidate_end):
    """Return False if moving would cross higher-priority punctuation/structure."""
    if candidate_end == current_end:
        return True

    first = min(current_end, candidate_end) + 1
    last = max(current_end, candidate_end)

    # Endpoints are allowed to be landing points; only boundaries strictly between
    # the old and new positions act as walls.
    for crossed in range(first, last):
        if _cadence_boundary_is_locked(tokens, crossed):
            return False

    return True


def _refine_spans_by_vtt_cadence(tokens, spans):
    """
    Refine already-selected DP boundaries using exact local VTT cadence.

    Cadence3.1 keeps the beta.8 philosophy but loosens one overly strict gate:
      * punctuation, speaker structure, and EarlyPunctStart3 still lock a boundary;
      * weak grammar preferences no longer lock the CURRENT DP boundary;
      * a NEW boundary must pass a stricter grammatical no-break gate;
      * only literal inline-VTT word starts are compared (no interpolation);
      * the search moves at most VTT_CADENCE_LOOKAROUND_WORDS in either direction;
      * no authoritative boundary may be crossed;
      * both resulting cues must remain valid and may not add any configured
        readability-threshold violation;
      * the current boundary itself need not have exact timing, but the local
        corridor must contain at least two comparable exact onset intervals;
      * the unique longest exact local interval wins; ties keep the DP decision.

    There is intentionally no millisecond pause threshold.
    """
    if len(spans) < 2:
        return spans, 0

    refined = [dict(span) for span in spans]
    refinements = 0

    for boundary_pos in range(len(refined) - 1):
        left = refined[boundary_pos]
        right = refined[boundary_pos + 1]
        current_end = int(left['end_index'])

        if int(right['start_index']) != current_end + 1:
            continue
        if _cadence_boundary_is_locked(tokens, current_end):
            continue

        left_start = int(left['start_index'])
        right_end = int(right['end_index'])
        first_candidate = max(left_start, current_end - VTT_CADENCE_LOOKAROUND_WORDS)
        last_candidate = min(right_end - 1, current_end + VTT_CADENCE_LOOKAROUND_WORDS)

        current_left_flags = _cadence_quality_flags(tokens, left_start, current_end)
        current_right_flags = _cadence_quality_flags(tokens, current_end + 1, right_end)
        if current_left_flags is None or current_right_flags is None:
            continue
        current_flags = tuple(
            a + b for a, b in zip(current_left_flags, current_right_flags)
        )

        # First determine the local cadence truth independently of grammar. We need
        # at least two exact source intervals to make a relative comparison. If the
        # strongest interval lands on an unacceptable grammatical split, we keep
        # the DP boundary rather than silently choosing the second-strongest pause.
        evidence = []
        for candidate_end in range(first_candidate, last_candidate + 1):
            if not _cadence_can_reach(tokens, current_end, candidate_end):
                continue
            interval = _exact_vtt_onset_interval(tokens, candidate_end)
            if interval is not None:
                evidence.append((interval, candidate_end))

        if len(evidence) < 2:
            continue

        longest = max(interval for interval, _ in evidence)
        winners = [
            end_index
            for interval, end_index in evidence
            if interval == longest
        ]
        if len(winners) != 1:
            continue

        best_end = winners[0]
        if best_end == current_end:
            continue
        if not _cadence_candidate_is_acceptable(tokens, best_end):
            continue

        new_left_flags = _cadence_quality_flags(tokens, left_start, best_end)
        new_right_flags = _cadence_quality_flags(tokens, best_end + 1, right_end)
        if new_left_flags is None or new_right_flags is None:
            continue

        new_flags = tuple(
            a + b for a, b in zip(new_left_flags, new_right_flags)
        )
        if any(new > old for new, old in zip(new_flags, current_flags)):
            continue

        left_candidate = _candidate_cost(tokens, left_start, best_end)
        right_candidate = _candidate_cost(tokens, best_end + 1, right_end)
        if left_candidate is None or right_candidate is None:
            continue

        left['end_index'] = best_end
        left['lines'] = left_candidate[1]
        right['start_index'] = best_end + 1
        right['lines'] = right_candidate[1]
        refinements += 1

    return refined, refinements

def _candidate_cost(tokens, start_index, end_index):
    # Never strand a structural marker at the end of one cue with its spoken
    # text in the next. A cue may start with >>, but it may not end with it.
    if tokens[end_index]['text'] == SPEAKER_MARKER:
        return None

    # If the previous token is >>, this candidate would start after a marker
    # that was detached into the preceding cue. Reject that path.
    if start_index > 0 and tokens[start_index - 1]['text'] == SPEAKER_MARKER:
        return None

    text = ' '.join(
        token['text'] for token in tokens[start_index:end_index + 1]
    )

    if len(text) > MAX_CUE_CHARS:
        return None

    lines = _wrap_linguistically(text)
    if not lines or len(lines) > MAX_LINES:
        return None

    start = tokens[start_index]['start']
    end = tokens[end_index]['end']
    duration = max(1, end - start)

    # VTT1.5 deliberately keeps stale rolling-window presentation time out of
    # ordinary DP scoring. One narrow exception is needed at the final token of
    # a speech region: if that stale source end would make every terminal
    # candidate impossible, the whole region would fall back to the greedy
    # segmenter and bypass the grammar/readability scoring entirely.
    #
    # For an already-recognized terminal VTT estimate, use the estimated speech
    # end for candidate feasibility/readability only. _cue_from_token_span still
    # retains the raw token end plus speech-end provenance, so final display timing
    # remains under the existing VTT1.5 normalization policy.
    scoring_duration = duration
    if duration > MAX_DURATION_MS + 500 and end_index == len(tokens) - 1:
        speech_end, speech_end_source = _best_terminal_speech_end(tokens, end_index)
        if speech_end_source in {'vtt_terminal_estimate', 'vtt_source_start_estimate'}:
            scoring_duration = max(1, int(speech_end) - int(start))

    # Do not create a cue wildly over the chosen maximum. A small amount of
    # slack is allowed in the search so later timing normalization has options.
    if scoring_duration > MAX_DURATION_MS + 500:
        return None

    cps = _cps(text, scoring_duration)
    wpm = _wpm(text, scoring_duration)
    words = end_index - start_index + 1

    # Small base cost discourages needless rapid-fire cues.
    cost = 18.0

    # A cue that starts mid-thought and immediately reaches a comma/semicolon/
    # colon is usually evidence that the prior boundary was a few words early.
    # Penalize that start once per candidate path; the DP remains free to keep it
    # when timing, line capacity, or readability make the alternative impossible.
    cost += _early_cue_medium_start_penalty(tokens, start_index)

    # Reading duration.
    if scoring_duration < MIN_DURATION_MS:
        shortage = (MIN_DURATION_MS - scoring_duration) / 100.0
        cost += 100 + shortage * shortage * 1.5
    elif scoring_duration > MAX_DURATION_MS:
        excess = (scoring_duration - MAX_DURATION_MS) / 100.0
        cost += 120 + excess * excess * 3.0

    # Reading speed: OPTIMAL_CPS is the target; MAX_CPS is the harder ceiling.
    if cps > MAX_CPS:
        cost += 120 + (cps - MAX_CPS) ** 2 * 10
    elif cps > OPTIMAL_CPS:
        cost += (cps - OPTIMAL_CPS) ** 2
    else:
        cost += (OPTIMAL_CPS - cps) * 0.08

    if wpm > MAX_WPM:
        cost += 60 + (wpm - MAX_WPM) ** 2 / 400.0

    # A completed sentence sitting inside a cue is usually evidence that the
    # cue should have ended there. Two very short sentences can still share a
    # cue when timing/readability makes that preferable.
    internal_sentence_ends = sum(
        1
        for index in range(start_index, end_index)
        if _ends_strongly(tokens[index]['text'])
    )
    cost += internal_sentence_ends * 95

    # Prefer a sensible end boundary.
    if end_index < len(tokens) - 1:
        cost -= _cue_boundary_score(tokens, end_index)
    else:
        cost -= 100

    # Avoid tiny orphan cues unless there really is no better option.
    if words <= 2 and len(tokens) > 3:
        cost += 45

    return cost, lines, text, start, end


def _cue_from_token_span(tokens, start_index, end_index, lines):
    """Build a segmented cue while retaining final-token speech-end provenance."""
    speech_end, speech_end_source = _best_terminal_speech_end(tokens, end_index)
    return {
        'start': tokens[start_index]['start'],
        'end': tokens[end_index]['end'],
        'lines': lines,
        'speech_end': speech_end,
        'speech_end_source': speech_end_source,
    }


def _fallback_segment_region(tokens):
    """Greedy safety net if the dynamic-programming search finds no path."""
    result = []
    index = 0

    while index < len(tokens):
        best = None

        for end_index in range(index, len(tokens)):
            # A marker belongs to the speech that follows it, never to the end
            # of the preceding cue.
            if tokens[end_index]['text'] == SPEAKER_MARKER:
                continue

            text = ' '.join(
                token['text'] for token in tokens[index:end_index + 1]
            )

            # Give marker-bearing text a few characters of harmless slack here;
            # _wrap_linguistically remains the configured line-layout authority.
            if len(text) > MAX_CUE_CHARS + 8:
                break

            if _wrap_linguistically(text):
                best = end_index

        if best is None:
            # Pathological fallback: if we are sitting on a marker, attach at
            # least one following token rather than outputting ">>" by itself.
            if (
                tokens[index]['text'] == SPEAKER_MARKER
                and index + 1 < len(tokens)
            ):
                best = index + 1
            else:
                best = index

        text = ' '.join(
            token['text'] for token in tokens[index:best + 1]
        )
        lines = _wrap_linguistically(text)

        if not lines:
            # Last-resort preservation for malformed/unbreakable input.
            # Keep the marker at line start; final rendering will still turn it
            # into "- " rather than allowing a mid-line >>.
            if text.startswith(SPEAKER_MARKER):
                body = text[len(SPEAKER_MARKER):].strip()
                lines = [SPEAKER_INTERNAL_PREFIX + body]
            else:
                lines = [text]

        result.append(_cue_from_token_span(
            tokens,
            index,
            best,
            lines,
        ))
        index = best + 1

    return result


def _segment_region(region):
    tokens = []
    for cue in region:
        tokens.extend(_tokenize_cue_with_times(cue))

    if not tokens:
        return []

    count = len(tokens)
    best_cost = [float('inf')] * (count + 1)
    previous = [None] * (count + 1)
    best_cost[0] = 0.0

    for start_index in range(count):
        if best_cost[start_index] == float('inf'):
            continue

        # 40 words is already far beyond what can normally fit in 84 chars,
        # but the cap prevents pathological quadratic work on malformed input.
        for end_index in range(start_index, min(count, start_index + 40)):
            candidate = _candidate_cost(tokens, start_index, end_index)

            if candidate is None:
                text = ' '.join(
                    token['text']
                    for token in tokens[start_index:end_index + 1]
                )
                if len(text) > MAX_CUE_CHARS:
                    break
                continue

            candidate_cost, lines, text, start, end = candidate
            total = best_cost[start_index] + candidate_cost

            if total < best_cost[end_index + 1]:
                best_cost[end_index + 1] = total
                previous[end_index + 1] = (
                    start_index,
                    end_index,
                    lines,
                    text,
                    start,
                    end,
                )

    if previous[count] is None:
        return _fallback_segment_region(tokens), 0

    spans = []
    cursor = count

    while cursor > 0:
        (
            start_index,
            end_index,
            lines,
            text,
            start,
            end,
        ) = previous[cursor]

        spans.append({
            'start_index': start_index,
            'end_index': end_index,
            'lines': lines,
        })
        cursor = start_index

    spans.reverse()
    spans, cadence_refinements = _refine_spans_by_vtt_cadence(tokens, spans)

    result = [
        _cue_from_token_span(
            tokens,
            span['start_index'],
            span['end_index'],
            span['lines'],
        )
        for span in spans
    ]
    return result, cadence_refinements


def _resegment_cues(cues, return_cadence_stats=False):
    regions = _build_speech_regions(cues)
    result = []
    cadence_refinements = 0

    for region_index, region in enumerate(regions):
        segmented, region_cadence_refinements = _segment_region(region)
        cadence_refinements += region_cadence_refinements
        if segmented:
            if region_index > 0:
                segmented[0]['hard_region_boundary_before'] = True
            if region[0].get('non_speech_boundary_before'):
                segmented[0]['non_speech_boundary_before'] = True
            if region[-1].get('non_speech_boundary_after'):
                segmented[-1]['non_speech_boundary_after'] = True
        result.extend(segmented)

    if return_cadence_stats:
        return result, len(regions), cadence_refinements
    return result, len(regions)


# ---------------------------------------------------------------------------
# Pass 3: repair fragments, resolve speech ends, normalize display timing, then wrap
# ---------------------------------------------------------------------------

def _merge_pair(left, right):
    if (
        left.get('non_speech_boundary_after')
        or right.get('non_speech_boundary_before')
        or right.get('hard_region_boundary_before')
    ):
        return None

    text = _normalize_text(f'{_cue_text(left)} {_cue_text(right)}')
    lines = _wrap_linguistically(text)

    if not lines:
        return None

    start = left['start']
    end = right['end']

    if end - start > MAX_DURATION_MS:
        return None

    merged = {
        'start': start,
        'end': end,
        'lines': lines,
        'speech_end': right.get('speech_end', right['end']),
        'speech_end_source': right.get('speech_end_source', 'legacy'),
    }
    if left.get('non_speech_boundary_before'):
        merged['non_speech_boundary_before'] = True
    if left.get('hard_region_boundary_before'):
        merged['hard_region_boundary_before'] = True
    if right.get('non_speech_boundary_after'):
        merged['non_speech_boundary_after'] = True
    return merged


def _merge_quality(cue):
    text = _cue_text(cue)
    duration = _duration_ms(cue)

    penalty = 0.0

    if duration < MIN_DURATION_MS:
        penalty += (MIN_DURATION_MS - duration) / 10.0

    cps = _cps(text, duration)
    if cps > MAX_CPS:
        penalty += (cps - MAX_CPS) * 10

    if _wpm(text, duration) > MAX_WPM:
        penalty += 20

    # Prefer a merged result that ends at a complete sentence.
    if _ends_strongly(text):
        penalty -= 12

    return penalty


def _repair_short_or_fast_cues(cues):
    """
    Merge pathological micro-cues / unreadably fast fragments with a neighbor
    when the combined subtitle still fits the configured readability profile.
    """
    cues = [
        {
            'start': cue['start'],
            'end': cue['end'],
            'lines': list(cue['lines']),
            'speech_end': cue.get('speech_end', cue['end']),
            'speech_end_source': cue.get('speech_end_source', 'legacy'),
            **({'non_speech_boundary_before': True} if cue.get('non_speech_boundary_before') else {}),
            **({'non_speech_boundary_after': True} if cue.get('non_speech_boundary_after') else {}),
            **({'hard_region_boundary_before': True} if cue.get('hard_region_boundary_before') else {}),
        }
        for cue in cues
    ]

    repairs = 0
    changed = True

    while changed:
        changed = False

        for index, cue in enumerate(cues):
            text = _cue_text(cue)
            duration = _duration_ms(cue)

            needs_repair = (
                duration < MIN_DURATION_MS
                or _cps(text, duration) > MAX_CPS
                or _wpm(text, duration) > MAX_WPM
            )

            if not needs_repair:
                continue

            options = []

            if index > 0:
                merged = _merge_pair(cues[index - 1], cue)
                if merged:
                    options.append(('previous', _merge_quality(merged), merged))

            if index + 1 < len(cues):
                merged = _merge_pair(cue, cues[index + 1])
                if merged:
                    options.append(('next', _merge_quality(merged), merged))

            if not options:
                continue

            direction, _score, merged = min(options, key=lambda item: item[1])

            if direction == 'previous':
                cues[index - 1:index + 1] = [merged]
            else:
                cues[index:index + 2] = [merged]

            repairs += 1
            changed = True
            break

    return cues, repairs


def _is_standalone_sound_event(cue):
    """Fallback detector for any removable sound event that reaches this late."""
    return _is_non_speech_annotation_text(_cue_text(cue))


def _resolve_final_cue_speech_ends(cues):
    """
    Finalize the distinction between speech timing and display timing.

    Normal spoken cues retain the final-token speech end carried out of the VTT
    segmentation stage. Standalone bracketed sound events are duration events,
    not spoken words, so their source cue duration is intentionally preserved.
    """
    resolved = []
    stats = {
        'vtt_terminal_estimates': 0,
        'vtt_source_start_estimates': 0,
        'sound_events_preserved': 0,
    }

    for cue in cues:
        new_cue = {
            'start': cue['start'],
            'end': cue['end'],
            'lines': list(cue['lines']),
            'speech_end': cue.get('speech_end', cue['end']),
            'speech_end_source': cue.get('speech_end_source', 'legacy'),
        }
        if cue.get('non_speech_boundary_before'):
            new_cue['non_speech_boundary_before'] = True
        if cue.get('non_speech_boundary_after'):
            new_cue['non_speech_boundary_after'] = True
        if cue.get('hard_region_boundary_before'):
            new_cue['hard_region_boundary_before'] = True

        if _is_standalone_sound_event(new_cue):
            new_cue['speech_end'] = new_cue['end']
            new_cue['speech_end_source'] = 'duration_event'
            stats['sound_events_preserved'] += 1
        else:
            new_cue['speech_end'] = max(
                new_cue['start'] + 1,
                min(new_cue['end'], int(new_cue['speech_end'])),
            )
            if new_cue['speech_end_source'] == 'vtt_terminal_estimate':
                stats['vtt_terminal_estimates'] += 1
            elif new_cue['speech_end_source'] == 'vtt_source_start_estimate':
                stats['vtt_source_start_estimates'] += 1

        resolved.append(new_cue)

    return resolved, stats


def _normalize_timings(cues):
    """
    Convert best-known speech timing into final subtitle display timing.

    VTT-backed spoken cues stay visible through the resolved speech end plus a
    short post-speech hold, or through the minimum readable duration plus its
    grace period, whichever is later. Standalone sound events preserve their
    source duration. Legacy/interpolated cues retain the pre-VTT1.4 behavior. VTT1.5 can also
    carry source-start-only speech-end evidence through that unchanged segmentation path.

    No cue may overlap the next cue, and spoken-text starts are never delayed to
    satisfy a numeric readability target.
    """
    cues = [
        {
            'start': cue['start'],
            'end': cue['end'],
            'lines': list(cue['lines']),
            'speech_end': cue.get('speech_end', cue['end']),
            'speech_end_source': cue.get('speech_end_source', 'legacy'),
            **({'non_speech_boundary_before': True} if cue.get('non_speech_boundary_before') else {}),
            **({'non_speech_boundary_after': True} if cue.get('non_speech_boundary_after') else {}),
            **({'hard_region_boundary_before': True} if cue.get('hard_region_boundary_before') else {}),
        }
        for cue in cues
    ]

    adjustments = 0

    for index, cue in enumerate(cues):
        text = _cue_text(cue)
        current_end = cue['end']
        speech_end = max(
            cue['start'] + 1,
            min(current_end, int(cue.get('speech_end', current_end))),
        )
        timing_source = cue.get('speech_end_source', 'legacy')

        if index + 1 < len(cues):
            hard_end = cues[index + 1]['start'] - MIN_GAP_MS
        else:
            hard_end = None

        if timing_source == 'duration_event':
            # [music], [screaming], etc. describe a continuing event rather than
            # a spoken word onset, so their source duration remains authoritative.
            target_end = current_end
        else:
            readable_end = cue['start'] + _minimum_readable_duration_ms(text)

            if timing_source.startswith('vtt_'):
                natural_end = max(
                    speech_end + POST_SPEECH_HOLD_MS,
                    readable_end + READABILITY_PADDING_MS,
                )
                # The display cap must never cut off known speech. If speech itself
                # exceeds MAX_DURATION_MS, preserve it but add no extra cap-breaking hold.
                display_cap = max(
                    speech_end,
                    cue['start'] + MAX_DURATION_MS,
                )
                target_end = min(natural_end, display_cap)
            else:
                # Preserve legacy fallback behavior when no usable inline VTT timing
                # exists; VTT1.4's speech-end shortening is deliberately source-driven.
                natural_end = max(
                    current_end,
                    readable_end + READABILITY_PADDING_MS,
                )
                display_cap = max(
                    current_end,
                    cue['start'] + MAX_DURATION_MS,
                )
                target_end = min(natural_end, display_cap)

        if hard_end is not None:
            target_end = min(target_end, hard_end)

            # Professional timed-text practice generally avoids very brief flashes
            # of subtitle-free screen between consecutive spoken captions. After
            # normal speech/readability timing is resolved, chain a spoken cue to
            # MIN_GAP_MS before the next spoken cue when the remaining blank gap is
            # under MAX_CHAIN_GAP_MS. Standalone sound events remain source-timed
            # duration events and do not participate in this chaining policy.
            next_timing_source = cues[index + 1].get('speech_end_source', 'legacy')
            if (
                timing_source != 'duration_event'
                and next_timing_source != 'duration_event'
                and not cue.get('non_speech_boundary_after')
                and not cues[index + 1].get('non_speech_boundary_before')
                and not cues[index + 1].get('hard_region_boundary_before')
            ):
                blank_gap = cues[index + 1]['start'] - target_end
                if 0 < blank_gap < MAX_CHAIN_GAP_MS:
                    target_end = hard_end

        target_end = max(cue['start'] + 1, target_end)
        if target_end != current_end:
            cue['end'] = target_end
            adjustments += 1

    return cues, adjustments


def _rewrap_all(cues):
    rewrapped = 0

    for cue in cues:
        text = _cue_text(cue)
        lines = _wrap_linguistically(text) or [text]

        if lines != cue['lines']:
            rewrapped += 1

        cue['lines'] = lines

    return cues, rewrapped


def _count_quality(cues):
    metrics = {
        'under_min_duration': 0,
        'over_max_duration': 0,
        'over_max_cps': 0,
        'over_max_wpm': 0,
        'line_over_max_chars': 0,
        'over_max_lines': 0,
        'two_line_cues': 0,
        'micro_under_100ms': 0,
    }

    for cue in cues:
        text = _cue_text(cue)
        duration = _duration_ms(cue)

        if duration < MIN_DURATION_MS:
            metrics['under_min_duration'] += 1
        if duration > MAX_DURATION_MS:
            metrics['over_max_duration'] += 1
        if _cps(text, duration) > MAX_CPS:
            metrics['over_max_cps'] += 1
        if _wpm(text, duration) > MAX_WPM:
            metrics['over_max_wpm'] += 1
        if any(len(line) > SINGLE_LINE_MAX for line in cue['lines']):
            metrics['line_over_max_chars'] += 1
        if len(cue['lines']) > MAX_LINES:
            metrics['over_max_lines'] += 1
        if len(cue['lines']) == 2:
            metrics['two_line_cues'] += 1
        if duration < 100:
            metrics['micro_under_100ms'] += 1

    return metrics


def _quality_advisory_parts(metrics):
    """Describe non-zero readability metrics using the active profile values."""
    parts = []
    if metrics['under_min_duration']:
        parts.append(
            f'{metrics["under_min_duration"]} cue(s) under {MIN_DURATION_MS} ms'
        )
    if metrics['over_max_duration']:
        parts.append(
            f'{metrics["over_max_duration"]} cue(s) over {MAX_DURATION_MS} ms'
        )
    if metrics['over_max_cps']:
        parts.append(f'{metrics["over_max_cps"]} cue(s) over {MAX_CPS} CPS')
    if metrics['over_max_wpm']:
        parts.append(f'{metrics["over_max_wpm"]} cue(s) over {MAX_WPM} WPM')
    if metrics['line_over_max_chars']:
        parts.append(
            f'{metrics["line_over_max_chars"]} line(s) over {SINGLE_LINE_MAX} chars'
        )
    if metrics['over_max_lines']:
        parts.append(f'{metrics["over_max_lines"]} cue(s) over {MAX_LINES} lines')
    if metrics['micro_under_100ms']:
        parts.append(f'{metrics["micro_under_100ms"]} cue(s) under 100 ms')
    return parts


def _count_final_invariants(cues):
    """Audit structural properties that should always hold in final output."""
    metrics = {
        'empty_cues': 0,
        'empty_lines': 0,
        'nonpositive_duration': 0,
        'overlaps': 0,
        'leftover_speaker_markers': 0,
    }

    for index, cue in enumerate(cues):
        lines = cue.get('lines') or []
        if not lines or not _cue_text(cue):
            metrics['empty_cues'] += 1
        metrics['empty_lines'] += sum(1 for line in lines if not line.strip())
        if cue.get('end', 0) <= cue.get('start', 0):
            metrics['nonpositive_duration'] += 1
        metrics['leftover_speaker_markers'] += sum(
            line.count(SPEAKER_MARKER) for line in lines
        )

        if index + 1 < len(cues) and cue['end'] >= cues[index + 1]['start']:
            metrics['overlaps'] += 1

    return metrics


def _atomic_write_text(filepath, text):
    """Replace the subtitle only after the complete cleaned file is written."""
    temp_path = f'{filepath}.cleanautosubs.{os.getpid()}.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, filepath)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

def _serialize_srt(cues):
    blocks = []

    for index, cue in enumerate(cues, 1):
        blocks.append(
            f'{index}\n'
            f'{_format_ms(cue["start"])} --> {_format_ms(cue["end"])}\n'
            + '\n'.join(cue['lines'])
        )

    return '\n\n'.join(blocks) + '\n'


class CleanAutoSubsPP(PostProcessor):
    """
    De-roll, linguistically re-segment, re-time, and reflow English YouTube
    automatic captions before FFmpegEmbedSubtitle embeds the cleaned SRT.
    """

    def run(self, info):
        requested_before_conversion = info.get('requested_subtitles') or {}
        automatic_captions = info.get('automatic_captions') or {}
        manual_subtitles = info.get('subtitles') or {}

        # Only alter tracks that actually came from automatic captions.
        # Creator-authored subtitles remain untouched.
        auto_languages = set()

        for language, selected in requested_before_conversion.items():
            selected_url = selected.get('url')
            candidates = automatic_captions.get(language) or []

            if selected_url and any(
                candidate.get('url') == selected_url
                for candidate in candidates
            ):
                auto_languages.add(language)
            elif language.endswith('-orig') and candidates:
                auto_languages.add(language)
            elif candidates and language not in manual_subtitles:
                auto_languages.add(language)

        # Capture WebVTT inline timing BEFORE conversion. FFmpeg's SRT output
        # necessarily retains only cue-level timing, so this is the last point at
        # which YouTube's intra-cue timestamp anchors are available.
        vtt_timing_sources, vtt_capture_errors = _capture_vtt_timing_sources(
            requested_before_conversion,
            auto_languages,
        )
        for language, detail in vtt_capture_errors:
            self.report_warning(
                f'Could not preserve source VTT timing for {language}; '
                f'using legacy interpolation instead ({detail})'
            )

        # Convert to SRT so the cleaner and downstream embedder still use the
        # same simple, deterministic intermediate format used by the cleaner.
        files_to_delete, info = FFmpegSubtitlesConvertorPP(
            self._downloader,
            'srt',
        ).run(info)

        subtitles = info.get('requested_subtitles') or {}
        if not subtitles:
            self.to_screen('No subtitles to clean')
            return files_to_delete, info

        for language, subtitle in subtitles.items():
            if language not in auto_languages:
                self.to_screen(
                    f'Leaving {language} creator subtitle unchanged'
                )
                continue

            if not _is_english_language(language):
                self.to_screen(
                    f'Leaving {language} automatic subtitle unchanged: '
                    'linguistic cleanup currently supports English'
                )
                continue

            if subtitle.get('ext') != 'srt':
                continue

            filepath = subtitle.get('filepath')
            if not filepath or not os.path.isfile(filepath):
                self.report_warning(
                    f'Skipping {language}: subtitle file was not found'
                )
                continue

            with open(filepath, 'r', encoding='utf-8-sig') as handle:
                original = handle.read()

            srt_cues = _parse_srt(original)
            if not srt_cues:
                self.report_warning(
                    f'Skipping {language}: no SRT cues could be parsed'
                )
                continue

            timing_mode = 'legacy interpolation (no usable inline VTT timing)'
            timing_stats = {
                'source_cues': 0,
                'timed_cues': 0,
                'timed_tokens': 0,
                'inline_anchors': 0,
                'window_resets': 0,
                'terminal_holds_trimmed': 0,
            }
            cues = srt_cues

            captured = vtt_timing_sources.get(language)
            if captured:
                vtt_cues = captured['cues']
                timing_stats = captured['stats']

                # Conservative sanity check against FFmpeg's converted SRT. If
                # our lightweight VTT parser disagrees substantially, retain the
                # proven SRT path rather than risking a malformed track.
                timing_is_sane, cue_delta, word_ratio = _vtt_timing_sanity(
                    srt_cues,
                    vtt_cues,
                )

                if timing_is_sane:
                    cues = vtt_cues
                    timing_mode = 'WebVTT inline timing'
                else:
                    timing_mode = 'legacy interpolation (captured VTT failed sanity check)'
                    self.report_warning(
                        f'Ignoring captured VTT timing for {language}: parser '
                        f'sanity check differed from converted SRT '
                        f'(cue delta {cue_delta}, word ratio {word_ratio:.3f})'
                    )

            original_count = len(cues)

            cues, removed, source_overlaps, duplicate_merges = (
                _clean_rolling_cues(cues)
            )
            derolled_count = len(cues)

            cues, annotation_stats = _strip_non_speech_annotations(cues)
            annotation_cleaned_count = len(cues)
            vtt_timed_cues_used = sum(1 for cue in cues if cue.get('timed_tokens'))
            vtt_timed_tokens_used = sum(
                len(cue.get('timed_tokens') or []) for cue in cues
            )

            cues, region_count, cadence_refinements = _resegment_cues(
                cues,
                return_cadence_stats=True,
            )
            resegmented_count = len(cues)

            cues, fragment_repairs = _repair_short_or_fast_cues(cues)
            cues, speech_end_stats = _resolve_final_cue_speech_ends(cues)
            cues, timing_adjustments = _normalize_timings(cues)
            cues, rewrapped = _rewrap_all(cues)

            # Only now convert structural >> markers to readable dash-dialogue.
            # Doing this at the end preserves marker boundaries through every
            # merge/reflow pass above.
            cues, speaker_markers_rendered = _render_speaker_markers(cues)

            metrics = _count_quality(cues)
            invariants = _count_final_invariants(cues)
            output = _serialize_srt(cues)

            _atomic_write_text(filepath, output)

            # Keep yt-dlp's in-memory subtitle data synchronized with the file
            # that FFmpegEmbedSubtitle consumes next.
            subtitle['data'] = output

            self.to_screen(
                'Readability profile: '
                f'{SINGLE_LINE_MAX} chars/line; {MAX_LINES} line(s); '
                f'{OPTIMAL_CPS} target CPS; {MAX_CPS} max CPS; '
                f'{MAX_WPM} max WPM; {MIN_DURATION_MS}-{MAX_DURATION_MS} ms; '
                f'{MIN_GAP_MS} ms minimum gap; {POST_SPEECH_HOLD_MS} ms post-speech hold; '
                f'{MAX_CHAIN_GAP_MS} ms max spoken-cue chain gap; '
                f'{READABILITY_PADDING_MS} ms readability padding'
            )

            self.to_screen(
                f'CleanAutoSubs v{PLUGIN_VERSION} + {GRAMMAR_LAYER_VERSION} + {TIMING_LAYER_VERSION} {language}: '
                f'{original_count} source -> {derolled_count} de-rolled -> '
                f'{annotation_cleaned_count} dialogue-source -> '
                f'{resegmented_count} re-segmented -> {len(cues)} final cues; '
                f'{removed} rolling line(s) removed; '
                f'{source_overlaps} source overlap(s) fixed; '
                f'{duplicate_merges} rolling duplicate cue(s) merged; '
                f'{annotation_stats["annotations_removed"]} non-speech annotation(s) stripped; '
                f'{annotation_stats["annotation_only_cues_removed"]} annotation-only source cue(s) removed; '
                f'{annotation_stats["hard_boundaries_preserved"]} non-speech hard boundary/boundaries preserved; '
                f'{annotation_stats["censor_markers_preserved"]} censorship marker(s) preserved; '
                f'{region_count} speech region(s); '
                f'{cadence_refinements} VTT cadence boundary refinement(s); '
                f'{fragment_repairs} short/fast fragment repair(s); '
                f'{rewrapped} cue(s) rewrapped; '
                f'{timing_adjustments} timing adjustment(s); '
                f'{speaker_markers_rendered} speaker marker(s) rendered as dash(es)'
            )

            self.to_screen(
                'Timing source: '
                f'{timing_mode}; '
                f'{vtt_timed_cues_used}/{annotation_cleaned_count} dialogue-source cue(s) '
                f'carried VTT timing; '
                f'{vtt_timed_tokens_used} VTT-timed token(s) used; '
                f'{timing_stats["inline_anchors"]} inline timestamp anchor(s) captured; '
                f'{timing_stats["window_resets"]} caption-window reset(s) preserved; '
                f'{timing_stats["terminal_holds_trimmed"]} reset-specific stale hold(s) trimmed; '
                f'{speech_end_stats["vtt_terminal_estimates"]} final-cue terminal hold(s) resolved; '
                f'{speech_end_stats["vtt_source_start_estimates"]} no-anchor final cue(s) source-timed; '
                f'{annotation_stats["vtt_timing_fallbacks"]} annotation-cleaned cue(s) fell back from VTT token timing; '
                f'{speech_end_stats["sound_events_preserved"]} late fallback sound event(s) preserved'
            )

            self.to_screen(
                'Final invariants: '
                f'{invariants["empty_cues"]} empty cue(s); '
                f'{invariants["nonpositive_duration"]} non-positive duration(s); '
                f'{invariants["overlaps"]} overlap(s); '
                f'{invariants["leftover_speaker_markers"]} leftover speaker marker(s)'
            )

            invariant_failures = {
                key: value for key, value in invariants.items() if value
            }
            if invariant_failures:
                self.report_warning(
                    f'Final subtitle invariant check found: {invariant_failures}'
                )

            self.to_screen(
                'Final quality: '
                f'{metrics["two_line_cues"]} two-line cue(s); '
                f'{metrics["under_min_duration"]} under {MIN_DURATION_MS} ms; '
                f'{metrics["over_max_duration"]} over {MAX_DURATION_MS} ms; '
                f'{metrics["micro_under_100ms"]} under 100 ms; '
                f'{metrics["over_max_cps"]} over {MAX_CPS} CPS; '
                f'{metrics["line_over_max_chars"]} line(s) over {SINGLE_LINE_MAX} chars'
            )

            advisory_parts = _quality_advisory_parts(metrics)
            if advisory_parts:
                self.report_warning(
                    'Readability advisory: '
                    + '; '.join(advisory_parts)
                    + '. Source timing was preserved rather than distorted solely to force the configured readability thresholds.'
                )

        return files_to_delete, info
