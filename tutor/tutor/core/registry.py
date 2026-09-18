"""Tool schemas exposed to the model (OpenAI function-calling shape).

The model chooses *what to ask about*; it can never choose a fact. Every tool
below returns data derived from the score, so the tutor's only honest move is to
read them out.
"""

from __future__ import annotations

TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'get_piece_map',
            'description': ('Overall map of the piece: bar count, key, key areas '
                            'with bar ranges, and verified repeated passages. '
                            'Call this before making any claim about form.'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_bar',
            'description': ('Facts for one bar: inferred chord, top note, note '
                            'count, onset rhythm, contour, and which key area it '
                            'sits in.'),
            'parameters': {
                'type': 'object',
                'properties': {'bar': {'type': 'integer',
                                       'description': 'Bar number as printed.'}},
                'required': ['bar'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'next_question',
            'description': ('Draw the next memorisation question. Returns the '
                            'prompt only -- never the answer. Tiers: 0 structure, '
                            '1 harmony, 2 surface.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'tier': {'type': 'integer', 'description': '0, 1 or 2'},
                    'type': {'type': 'string',
                             'description': 'optional question type filter'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'grade_answer',
            'description': ('Grade the student\'s answer to the pending question '
                            'and get an explanation. Accepts loose phrasings '
                            '("m53", "measure 53", "53").'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'answer': {'type': 'string',
                               'description': 'what the student said or played'},
                    'question_id': {'type': 'string'},
                },
                'required': ['answer'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'compare_played',
            'description': ('Compare the notes the student just played against '
                            'the score for a bar range. Pitch classes only; '
                            'timing is not assessed, so never claim timing.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'bars': {'type': 'array', 'items': {'type': 'integer'}},
                    'played_pitches': {'type': 'array', 'items': {'type': 'integer'},
                                       'description': 'MIDI note numbers'},
                },
                'required': ['bars', 'played_pitches'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'session_status',
            'description': 'Attempts so far, accuracy and current weak spots.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_mode',
            'description': ('Switch tutoring mode when the student asks: teach, '
                            'drill, listen_coach, converse, review.'),
            'parameters': {
                'type': 'object',
                'properties': {'mode': {'type': 'string'}},
                'required': ['mode'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'practice_render',
            'description': ('Write a MIDI file of a bar range at a steady tempo, '
                            'for practising against a pulse.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'from_bar': {'type': 'integer'},
                    'to_bar': {'type': 'integer'},
                    'qpm': {'type': 'number',
                            'description': 'steady tempo; omit for a sensible default'},
                },
                'required': ['from_bar', 'to_bar'],
            },
        },
    },
]

TOOL_NAMES = [t['function']['name'] for t in TOOL_SPECS]
