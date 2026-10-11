"""Step 11: export A3 maps, sensitivity charts and concise methodology."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.analysis_maps import make_maps_and_notes

if __name__=='__main__':
    make_maps_and_notes()
