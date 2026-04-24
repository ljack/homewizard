# Smart Plug Move Workflow

Kasa plug identity follows the physical plug MAC address, not the appliance.
When a plug moves, close the old appliance assignment first, then start the new
one after the plug is installed on the new appliance.

## Current Plugs

Use this to see tracked assignments:

```bash
python3 appliance_profiles.py --db p1_data.db --list-assignments
```

If the assignment table is empty, seed it from current plug labels:

```bash
python3 appliance_profiles.py --db p1_data.db --init-assignments --list-assignments
```

## Before Moving A Plug

Tell the app which physical plug is about to move. This snapshots the old
appliance profile and closes that assignment.

```bash
python3 appliance_profiles.py \
  --db p1_data.db \
  --before-move kasa <plug-mac> \
  --label "4:jääkaappi" \
  --notes "Moved fridge plug to another appliance"
```

The snapshot is stored in `appliance_profile_snapshots` and can later be used as
a virtual/estimated appliance profile.

## After Moving A Plug

Start a new assignment for the same physical plug:

```bash
python3 appliance_profiles.py \
  --db p1_data.db \
  --after-move kasa <plug-mac> "dishwasher" \
  --notes "Plug moved after fridge profile was captured"
```

## Finding The Plug ID

The real Kasa MAC addresses are kept in the local SQLite database and should not
be committed to Git. List them locally with:

```bash
python3 appliance_profiles.py --db p1_data.db --list-assignments
```

## Notes

Use the current time by default. If the physical move happened earlier, pass
`--at 2026-04-24T20:00:00` so the assignment boundary matches reality.

Keep the plug on a new appliance for at least a few hours for simple loads, and
preferably 24-72 hours for cycling appliances such as fridges, freezers,
dehumidifiers, washing machines, and dishwashers.
