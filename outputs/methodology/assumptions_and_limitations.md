# Assumptions and limitations

- Fixed walking speed 4.8 km/h; no slope, stairs, crossing-delay, congestion or mobility variation. Sensitivity varies time and buffer width, not walking speed.
- A/B are plausible reviewed external accesses, not confirmed future entrances. No internal-site walk is included.
- Three manually reviewed network connections model confirmed physical links; their geometry and lengths are modelling assumptions documented in the change log.
- OSM completeness, permissions and grade separation remain imperfect. Wider local gap, disconnected-component and conditional-access findings are retained, not silently repaired. A zero count of restricted links does not establish that every remaining route is publicly usable.
- The 1D network routing is authoritative. The final C1 surface is an ordinary-Delaunay interpolation proxy with a 150 m maximum triangle-edge filter; it is not a constrained-Delaunay reproduction. Water subtraction removes mapped water only; no claim of parcel-level access or complete barrier treatment is made.
- Census 2021 baseline population is uniformly allocated within complete zones (or complete mapped land for sensitivity). Neither method measures residential occupancy at building level or current population.
- Population is sensitive to within-LSOA allocation. Present the transparent P1 Census area-weighted result with the P2–P4 OS NGD sensitivity range.
- All school destination classifications remain pending unless evidence is entered in the review file. Address matches and polygon inclusion are not entrance verification. Candidate times are not school accessibility results.
- Water completeness follows the selected OS NGD or OSM source and its recorded acquisition date. OS Maps raster tiles, when configured, provide visual context only and never enter the analysis.
- Presentation rounds population to the nearest ten, area/density to one decimal; analytical CSV values retain precision.
