#!/bin/zsh --no-rcs
# Double-click to preview the dashboard (sample data) in the Try Alfred UI workflow.
# Replaces the clipboard with the preview JSON, which Try Alfred UI loads on opening.
set -e
cd -- "${0:A:h}"
/usr/bin/python3 make_preview.py
/usr/bin/pbcopy < dashboard-preview.json
/usr/bin/osascript -e 'tell application id "com.runningwithcrayons.Alfred" to search "tryaui"'
echo "Preview JSON copied. Press Return in Alfred to open Try Alfred UI."
