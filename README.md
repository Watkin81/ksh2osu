# ksh2osu
Convert ksh format maps to osu!'s .osz format 

Run with:
python ksh2osu.py <input.ksh> [output.osz] [--4k] [--offset MILLISECONDS]

You can optionally provide an output destination, skip fx notes and just convetr chip notes, and provide a custom offset in milliseconds.

This converter does not handle bpm changes yet.
This converter does not convert lasers, only chip and fx notes and holds to a 4/6k format.
Ensure the .ksh file has a song in the same folder.
