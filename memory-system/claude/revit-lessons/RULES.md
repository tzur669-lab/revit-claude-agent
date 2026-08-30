# revit lessons | v1 | ONLY user-corrected or user-approved | never self-authored
# format: id :: trigger :: rule :: check :: src :: ev
# see skills/revit-session/references/lessons.md for the gate, trigger design, locking, cap/dedup

domains: level-creation · wall-layout · room-definition · floor-slab ·
  staircase-layout · family-placement · door-window-placement · mep-routing ·
  view-creation · view-export · schedule-sheet · annotation-dimension ·
  hebrew-text-io · attribution-reporting · geometry-verification ·
  user-reporting · tracker-dev

L001 :: staircase-layout :: Israeli residential code caps an interior riser at 175mm and floors the going at 260mm; derive the shaft footprint from the resulting run length BEFORE laying out the rooms around it, never size the shaft first and fit the flight into it :: risers = ceil(floor_to_floor/175); pick going so 2R+G lands in 610-650; assert shaft_length >= longest_flight_run + landing_depth :: user-corrected 2026-08-30 :: -
