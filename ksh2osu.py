#!/usr/bin/env python3
"""
ksh_to_osz.py
Extended KSH -> osu!mania converter that creates a full .osz package.

- Parses KSH metadata (title, artist, difficulty, illustrator, jacket, bg, audio).
- Converts BT/FX notes into osu!mania 6K (or optional 4K).
- Generates a complete .osu file with filled metadata and timing.
- Bundles .osu, audio, and images into a .osz archive for direct import into osu!.

Usage:
    python ksh_to_osz.py <input.ksh> [output.osz] [--4k]
"""

import os
import sys
import math
import zipfile
import shutil
import argparse
from typing import Dict, List, Tuple, Optional

class KSHConverter:
    def __init__(self, four_key: bool = False, custom_offset_ms: int = 0):
        self.four_key = four_key
        self.custom_offset_ms = custom_offset_ms
        self.num_lanes = 4 if four_key else 6
        
    def parse_ksh_metadata(self, lines: List[str]) -> Tuple[Dict[str, str], int]:
        """Parse KSH metadata from header lines."""
        meta = {}
        i = 0
        
        while i < len(lines) and lines[i].strip() != '--':
            line = lines[i].strip()
            if '=' in line and not line.startswith('#'):
                try:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Remove BOM from the first key if present
                    if key.startswith('\ufeff'):
                        key = key[1:]  # Remove the BOM character
                    
                    meta[key] = value
                except ValueError:
                    pass
            i += 1
            
        return meta, i + 1 if i < len(lines) else i
    
    def ksh_to_osu_lane(self, is_fx: bool, idx: int) -> Optional[int]:
        """Convert KSH lane index to osu!mania lane."""
        if self.four_key:
            if is_fx:
                return None  # Skip FX notes in 4K mode
            return min(idx, 3)  # Clamp to 4 lanes
        else:
            if is_fx:
                return 0 if idx == 0 else 5  # FX-L to lane 0, FX-R to lane 5
            else:
                return 1 + min(idx, 3)  # BT lanes map to 1,2,3,4
    
    def lane_x_pos(self, lane_idx: int) -> int:
        """
        Calculate x position for osu!mania lane.
        
        Based on osu! documentation: column = floor(x * columnCount / 512)
        So to get x for a specific column: x = column * 512 / columnCount + offset
        
        We use the center of each column for better accuracy.
        """
        # Calculate the width of each column in the 512-unit space
        col_width = 512.0 / self.num_lanes
        
        # Calculate x position as the center of the lane
        x = lane_idx * col_width + col_width * 0.5
        
        return int(round(x))
    
    def parse_chart_data(self, lines: List[str], start_idx: int, meta: Dict[str, str]) -> Tuple[List[str], List[str]]:
        """Parse chart data and generate timing points and hit objects."""
        # Initialize timing variables with validation
        current_bpm = float(meta.get('t', 120))
        beats_per_measure = 4
        
        # Fix offset parsing - validate and use reasonable defaults
        raw_offset = meta.get('o', '0')
        try:
            parsed_offset = float(raw_offset or 0)
            # If offset seems unreasonable (over 30 seconds), ignore it
            if abs(parsed_offset) > 30000:
                print(f"Warning: Large offset {parsed_offset}ms detected, using 0ms instead")
                parsed_offset = 0
        except (ValueError, TypeError):
            parsed_offset = 0
            
        start_time = int(parsed_offset) + self.custom_offset_ms
        current_time = start_time
        
        # Output arrays
        timing_points = []
        hit_objects = []
        
        # Add initial timing point
        ms_per_beat = 60000.0 / current_bpm
        timing_points.append(f"{start_time},{ms_per_beat:.6f},4,1,0,100,1,0")
        
        # Hold state tracking - track hold start times
        hold_state = {lane: None for lane in range(self.num_lanes)}
        
        # Parse measures
        i = start_idx
        while i < len(lines):
            measure_lines = []
            measure_start_time = current_time
            measure_bpm = current_bpm
            measure_beats = beats_per_measure
            
            # Collect all lines in this measure
            while i < len(lines) and lines[i].strip() != '--':
                line = lines[i].strip()
                i += 1
                
                if not line:
                    continue
                
                # Skip fx-l= and fx-r= lines
                if line.startswith('fx-l=') or line.startswith('fx-r='):
                    continue
                    
                # Handle BPM changes
                if line.startswith('t='):
                    try:
                        new_bpm = float(line[2:])
                        if abs(new_bpm - current_bpm) > 0.001:
                            current_bpm = new_bpm
                            ms_per_beat = 60000.0 / current_bpm
                            timing_points.append(f"{int(current_time)},{ms_per_beat:.6f},4,1,0,100,1,0")
                    except ValueError:
                        pass
                    continue
                
                # Handle time signature changes
                if line.startswith('beat='):
                    try:
                        beat_str = line[5:]
                        if '/' in beat_str:
                            numerator = int(beat_str.split('/')[0])
                            beats_per_measure = numerator
                        else:
                            beats_per_measure = int(beat_str)
                    except ValueError:
                        pass
                    continue
                
                # Skip other control lines
                if '=' in line:
                    continue
                    
                measure_lines.append(line)
            
            # Process measure
            if measure_lines:
                note_lines = [ln for ln in measure_lines if '|' in ln]
                if note_lines:
                    current_time = self.process_measure(
                        note_lines, measure_start_time, current_bpm, 
                        beats_per_measure, hold_state, hit_objects
                    )
                else:
                    # Empty measure, advance time
                    measure_duration = (60000.0 / current_bpm) * beats_per_measure
                    current_time += measure_duration
            
            # Move to next measure
            if i < len(lines):
                i += 1  # Skip the '--' delimiter
        
        # Close any remaining hold notes
        self.close_remaining_holds(hold_state, hit_objects, int(current_time))
        
        return timing_points, hit_objects
    
    def process_measure_with_lookahead(self, note_lines: List[str], start_time: float, bpm: float, 
                                      beats_per_measure: int, hold_state: Dict[int, Optional[int]], 
                                      hit_objects: List[str], next_measure_data: Optional[List[str]]) -> float:
        """Process a single measure with lookahead for cross-measure holds."""
        if not note_lines:
            return start_time
            
        measure_duration = (60000.0 / bpm) * beats_per_measure
        line_duration = measure_duration / len(note_lines)
        
        # Process all lines in this measure
        for line_idx, line in enumerate(note_lines):
            line_time = int(start_time + line_idx * line_duration)
            
            # Split BT and FX parts - ignore everything after second |
            parts = line.split('|')
            bt_part = parts[0] if len(parts) > 0 else ''
            fx_part = parts[1] if len(parts) > 1 else ''
            
            # Process BT notes (lanes 0-3)
            for bt_idx, char in enumerate(bt_part):
                if bt_idx >= 4:  # KSH only has 4 BT lanes
                    break
                self.process_bt_note(char, bt_idx, line_time, hold_state, hit_objects)
            
            # Process FX notes (lanes 0-1 in FX, map to osu lanes 0 and 5)
            for fx_idx, char in enumerate(fx_part):
                if fx_idx >= 2:  # KSH only has 2 FX lanes
                    break
                self.process_fx_note(char, fx_idx, line_time, hold_state, hit_objects)
        
        # Check for cross-measure hold endings
        if next_measure_data:
            self.check_cross_measure_holds(hold_state, hit_objects, next_measure_data, int(start_time + measure_duration))
        
        return start_time + measure_duration
    
    def check_cross_measure_holds(self, hold_state: Dict[int, Optional[int]], 
                                 hit_objects: List[str], next_measure_data: List[str], 
                                 measure_end_time: int) -> None:
        """Check if holds should end at measure boundary based on next measure's first line."""
        if not next_measure_data:
            return
            
        # Get the first note line of the next measure
        first_line = None
        for line in next_measure_data:
            if '|' in line:
                first_line = line
                break
                
        if not first_line:
            return
            
        # Split BT and FX parts
        parts = first_line.split('|')
        bt_part = parts[0] if len(parts) > 0 else ''
        fx_part = parts[1] if len(parts) > 1 else ''
        
        # Check BT lanes for hold endings
        for bt_idx, char in enumerate(bt_part):
            if bt_idx >= 4:
                break
                
            osu_lane = self.ksh_to_osu_lane(False, bt_idx)
            if osu_lane is None:
                continue
                
            # If there's an active hold and next measure starts with 0 or 1, end the hold
            if hold_state[osu_lane] is not None and char in ['0', '1', ' ']:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                x = self.lane_x_pos(osu_lane)
                hit_objects.append(f"{x},192,{start_time},128,0,{measure_end_time}")
        
        # Check FX lanes for hold endings
        for fx_idx, char in enumerate(fx_part):
            if fx_idx >= 2:
                break
                
            osu_lane = self.ksh_to_osu_lane(True, fx_idx)
            if osu_lane is None:
                continue
                
            # If there's an active hold and next measure starts with 0 or 2, end the hold
            if hold_state[osu_lane] is not None and char in ['0', '2', ' ']:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                x = self.lane_x_pos(osu_lane)
                hit_objects.append(f"{x},192,{start_time},128,0,{measure_end_time}")
    
    def process_measure(self, note_lines: List[str], start_time: float, bpm: float, 
                       beats_per_measure: int, hold_state: Dict[int, Optional[int]], 
                       hit_objects: List[str]) -> float:
        """Legacy process_measure method - kept for compatibility."""
        return self.process_measure_with_lookahead(note_lines, start_time, bpm, beats_per_measure, hold_state, hit_objects, None)
    
    def process_bt_note(self, char: str, ksh_idx: int, time: int, 
                       hold_state: Dict[int, Optional[int]], hit_objects: List[str]) -> None:
        """Process a BT note according to new ruleset: 1=normal, 2=hold, 0=empty."""
        osu_lane = self.ksh_to_osu_lane(False, ksh_idx)
        if osu_lane is None:
            return
            
        x = self.lane_x_pos(osu_lane)
        
        if char == '1':  # Normal note
            # End any existing hold first
            if hold_state[osu_lane] is not None:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                hit_objects.append(f"{x},192,{start_time},128,0,{time}")
            
            # Create normal note
            hit_objects.append(f"{x},192,{time},1,0,0:0:0:0:")
            
        elif char == '2':  # Hold note or hold continuation
            if hold_state[osu_lane] is None:
                # Start new hold
                hold_state[osu_lane] = time
            # If already holding, continue (do nothing - hold continues)
            
        elif char == '0' or char == ' ':  # Empty - end any active hold
            if hold_state[osu_lane] is not None:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                hit_objects.append(f"{x},192,{start_time},128,0,{time}")
    
    def process_fx_note(self, char: str, ksh_idx: int, time: int,
                       hold_state: Dict[int, Optional[int]], hit_objects: List[str]) -> None:
        """Process an FX note according to new ruleset: 1=hold, 2=normal, 0=empty."""
        osu_lane = self.ksh_to_osu_lane(True, ksh_idx)
        if osu_lane is None:
            return
            
        x = self.lane_x_pos(osu_lane)
        
        if char == '2':  # Normal note for FX
            # End any existing hold first
            if hold_state[osu_lane] is not None:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                hit_objects.append(f"{x},192,{start_time},128,0,{time}")
            
            # Create normal note
            hit_objects.append(f"{x},192,{time},1,0,0:0:0:0:")
            
        elif char == '1':  # Hold note or hold continuation for FX
            if hold_state[osu_lane] is None:
                # Start new hold
                hold_state[osu_lane] = time
            # If already holding, continue (do nothing - hold continues)
            
        elif char == '0' or char == ' ':  # Empty - end any active hold
            if hold_state[osu_lane] is not None:
                start_time = hold_state[osu_lane]
                hold_state[osu_lane] = None
                hit_objects.append(f"{x},192,{start_time},128,0,{time}")
    
    def close_remaining_holds(self, hold_state: Dict[int, Optional[int]], 
                             hit_objects: List[str], final_time: int) -> None:
        """Close any remaining active hold notes."""
        for lane, start_time in hold_state.items():
            if start_time is not None:
                x = self.lane_x_pos(lane)
                hit_objects.append(f"{x},192,{start_time},128,0,{final_time}")
                hold_state[lane] = None
    
    def create_osu_content(self, meta: Dict[str, str], timing_points: List[str], 
                          hit_objects: List[str]) -> str:
        """Generate the complete .osu file content."""
        # Extract metadata with defaults
        title = meta.get('title', 'Untitled')
        artist = meta.get('artist', 'Unknown Artist')
        creator = meta.get('effect', 'ksh2osu')
        difficulty = meta.get('difficulty', 'converted')
        level = meta.get('level', '')
        version = f"{difficulty} {level}".strip()
        
        audio_file = meta.get('m', 'audio.ogg')
        illustrator = meta.get('illustrator', '')
        
        keycount = "4" if self.four_key else "6"
        
        lines = [
            "osu file format v14",
            "",
            "[General]",
            f"AudioFilename: {os.path.basename(audio_file)}",
            "AudioLeadIn: 0",
            "PreviewTime: -1",
            "Countdown: 0",
            "SampleSet: Normal",
            "StackLeniency: 0.7",
            "Mode: 3",
            "LetterboxInBreaks: 0",
            "SpecialStyle: 0",
            "WidescreenStoryboard: 0",
            "",
            "[Editor]",
            "DistanceSpacing: 1",
            "BeatDivisor: 4",
            "GridSize: 4",
            "TimelineZoom: 1",
            "",
            "[Metadata]",
            f"Title:{title}",
            f"TitleUnicode:{title}",
            f"Artist:{artist}",
            f"ArtistUnicode:{artist}",
            f"Creator:{creator}",
            f"Version:{version} {keycount}K",
            "Source:",
            f"Tags:{illustrator} {keycount}K KSH".strip(),
            "BeatmapID:0",
            "BeatmapSetID:-1",
            "",
            "[Difficulty]",
            "HPDrainRate:7",
            f"CircleSize:{keycount}",
            "OverallDifficulty:8",
            "ApproachRate:5",
            "SliderMultiplier:1.4",
            "SliderTickRate:1",
            "",
            "[Events]",
            "//Background and Video events",
            "//Break Periods",
            "//Storyboard Layer 0 (Background)",
            "//Storyboard Layer 1 (Fail)",
            "//Storyboard Layer 2 (Pass)",
            "//Storyboard Layer 3 (Foreground)",
            "//Storyboard Layer 4 (Overlay)",
            "//Storyboard Sound Samples",
        ]
        
        # Add background image if available
        bg_file = meta.get('bg', '')
        if bg_file:
            lines.append(f'0,0,"{os.path.basename(bg_file)}",0,0')
        
        lines.extend([
            "",
            "[TimingPoints]"
        ])
        lines.extend(timing_points)
        
        lines.extend([
            "",
            "[HitObjects]"
        ])
        lines.extend(hit_objects)
        
        return "\n".join(lines)
    
    def convert_ksh_to_osu(self, ksh_path: str, osu_path: str) -> Dict[str, str]:
        """Convert KSH file to osu! beatmap file."""
        try:
            with open(ksh_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = [line.rstrip('\r\n') for line in f]
        except Exception as e:
            raise Exception(f"Failed to read KSH file: {e}")
        
        # Parse metadata
        meta, chart_start = self.parse_ksh_metadata(lines)
        
        # Parse chart data
        timing_points, hit_objects = self.parse_chart_data(lines, chart_start, meta)
        
        # Generate osu! content
        osu_content = self.create_osu_content(meta, timing_points, hit_objects)
        
        # Write osu! file
        try:
            with open(osu_path, 'w', encoding='utf-8') as f:
                f.write(osu_content)
        except Exception as e:
            raise Exception(f"Failed to write osu! file: {e}")
        
        return meta

def build_osz_package(ksh_path: str, osu_path: str, meta: Dict[str, str], osz_path: str) -> str:
    """Build the final .osz package with all required files."""
    try:
        with zipfile.ZipFile(osz_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            # Add the .osu file
            z.write(osu_path, os.path.basename(osu_path))
            
            # Add referenced files (audio, images)
            folder = os.path.dirname(ksh_path)
            file_keys = ['m', 'jacket', 'bg', 'icon']
            
            for key in file_keys:
                if key in meta and meta[key]:
                    filename = meta[key]
                    filepath = os.path.join(folder, filename)
                    
                    if os.path.exists(filepath):
                        try:
                            z.write(filepath, os.path.basename(filename))
                            print(f"Added {filename} to package")
                        except Exception as e:
                            print(f"Warning: Could not add {filename}: {e}")
                    else:
                        print(f"Warning: Referenced file not found: {filepath}")
        
        return osz_path
        
    except Exception as e:
        raise Exception(f"Failed to create .osz package: {e}")

def main():
    parser = argparse.ArgumentParser(description='Convert KSH files to osu!mania .osz packages')
    parser.add_argument('input_ksh', help='Input KSH file path')
    parser.add_argument('output_osz', nargs='?', help='Output OSZ file path (optional)')
    parser.add_argument('--4k', action='store_true', dest='four_key', help='Convert to 4K instead of 6K')
    parser.add_argument('--offset', type=int, default=0, help='Custom offset in milliseconds')
    
    args = parser.parse_args()
    
    # Validate input file
    if not os.path.exists(args.input_ksh):
        print(f"Error: Input file '{args.input_ksh}' not found!")
        sys.exit(1)
    
    # Set output paths
    base_name = os.path.splitext(args.input_ksh)[0]
    output_osz = args.output_osz or f"{base_name}.osz"
    temp_osu = f"{base_name} (converted).osu"
    
    try:
        # Create converter
        converter = KSHConverter(four_key=args.four_key, custom_offset_ms=args.offset)
        
        print(f"Converting {args.input_ksh}...")
        print(f"Mode: {'4K' if args.four_key else '6K'}")
        if args.offset != 0:
            print(f"Offset: {args.offset}ms")
        
        # Convert KSH to osu!
        meta = converter.convert_ksh_to_osu(args.input_ksh, temp_osu)
        print(f"Generated {temp_osu}")
        
        # Build .osz package
        build_osz_package(args.input_ksh, temp_osu, meta, output_osz)
        print(f"Created {output_osz}")
        
        # Clean up temporary file
        try:
            os.remove(temp_osu)
        except:
            pass
        
        # Display song info
        print(f"\nSong Information:")
        print(f"  Title: {meta.get('title', 'Unknown')}")
        print(f"  Artist: {meta.get('artist', 'Unknown')}")
        print(f"  Difficulty: {meta.get('difficulty', 'Unknown')} {meta.get('level', '')}")
        print(f"  BPM: {meta.get('t', '120')}")
        print(f"  Offset: {meta.get('o', '0')}ms")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()