# Scheduling steward sweeps

Steward ships **no scheduler**: `steward-sweep` is a one-shot command with
machine-readable results, and recurring runs belong to the scheduler your
operating system already has. That is a deliberate boundary — there is no
daemon, watch, or interval mode, and the parser refuses to grow one (a test
pins it).

Every recipe below runs the same command:

```bash
recallweave steward-sweep /path/to/sources.json --vault /path/to/vault
```

Exit codes are documented in [steward.md](steward.md): `0` no change, `3`
findings recorded, `4` proposals awaiting your review, `5`/`6` only when you
opted into `--apply`. A second run that overlaps a running sweep — scheduled
against manual, or two schedules — refuses safely on the state lock with exit
code `2`; nothing is corrupted, and the next scheduled run proceeds normally.
Prefer scheduling outside your usual editing hours: a note saved mid-sweep is
simply carried to the next run (`changed_during_observe`), but quiet hours
give cleaner reports.

`--apply` in a scheduled sweep is opt-in twice over: the flag itself plus a
write policy in which you explicitly marked append-only mutation classes
`auto_apply`. Everything else stays pending for an interactive
`steward-apply`.

## macOS (launchd)

Save as `~/Library/LaunchAgents/com.recallweave.steward-sweep.plist`, then
`launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.recallweave.steward-sweep</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/recallweave</string>
    <string>steward-sweep</string>
    <string>/Users/you/steward/sources.json</string>
    <string>--vault</string>
    <string>/Users/you/Vault</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>6</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/you/steward/last-sweep.json</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/steward/last-sweep.err</string>
</dict>
</plist>
```

## Linux (cron)

```cron
0 6 * * 0  /usr/local/bin/recallweave steward-sweep /home/you/steward/sources.json --vault /home/you/vault > /home/you/steward/last-sweep.json 2> /home/you/steward/last-sweep.err
```

## Windows (Task Scheduler)

```powershell
schtasks /Create /TN "RecallWeave steward sweep" /SC WEEKLY /D SUN /ST 06:00 `
  /TR "\"C:\Python\Scripts\recallweave.exe\" steward-sweep \"C:\steward\sources.json\" --vault \"C:\Vault\""
```

Check the newest report any time with:

```bash
recallweave steward-status /path/to/sources.json
```
