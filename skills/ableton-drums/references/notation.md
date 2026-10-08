# Groove notation

One line per voice and one character per 16th note. A 4/4 bar is 16 characters; `|` marks
beats for readability and is ignored. For other meters, use (beats per bar × 4) steps: 9/8 = 18 steps.

```
H  x.x.|x.x.|x.x.|x.x.     step 0 = beat 1, step 4 = beat 2, ...
S  ....|X...|....|X...
K  x...|....|x.x.|....
```

## Voices
| code | voice | code | voice |
|---|---|---|---|
| K | kick | H | closed hi-hat |
| S | snare | O | open hi-hat |
| X | sidestick / rim | P | pedal hi-hat |
| R | ride (edge/bow) | B | ride bell |
| C | crash | N | china |
| T1 | high tom | T2 | mid tom |
| T3 | low tom | F | floor tom |

## Hits → velocity
| char | meaning | velocity |
|---|---|---|
| `X` | accent | 115–127 |
| `x` | normal | 90–110 |
| `o` | soft / timekeeping | 65–85 |
| `g` | ghost | 25–45 |
| `f` | flam: a grace note ~0.03 beats earlier at ~55, then the main hit | 100 |
| `r` | 32nd-note double (two hits half a step apart) | 85–100 |
| `.` | rest | |

## Converting to notes
For each bar `b` (0-based in the clip) and each step `i` with a hit:
`start = b * beats_per_bar + i * 0.25`, `duration = 0.25`, `pitch` = the pad mapped to the voice.
Triplet grooves are noted with 12 steps per bar (`step = 1/3` beat) and marked **(triplets)**.
