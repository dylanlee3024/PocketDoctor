# HEARTBEAT.md

tasks:

- name: daily-feelings-synthesis
  interval: 1d
  prompt: "Review the newest daily health log, recent meals, training entries, supplements, WHOOP summaries, and any symptom or mood reports. If there is enough signal, append a concise note to the latest `health/logs/YYYY-MM-DD.md` file explaining likely drivers of Dill's current energy, soreness, hunger, mood, GI status, or readiness. Cross-reference sleep, strain, food intake, hydration, recent training, illness, and routine changes. Label uncertainty clearly and only promote recurring patterns to `MEMORY.md` or structured health files. If there is no meaningful new signal, reply HEARTBEAT_OK."

- name: whoop-dream-consolidation
  interval: 1d
  prompt: "Review `health/whoop/latest.md`, the newest files under `health/whoop/daily/`, and recent WHOOP body data. Distill only durable signal, not daily noise. Update `health/biometrics.md` with stable device-derived trends such as body-weight drift, resting heart rate baseline, HRV baseline, sleep consistency patterns, recurring strain-recovery relationships, and notable changes that have repeated across multiple days. If a WHOOP-derived pattern is clearly durable and likely to matter in future coaching, also promote it into `MEMORY.md`. Append a short dated note to `DREAMS.md` summarizing what was consolidated or note that there was no durable WHOOP signal yet. If there is no meaningful new signal, reply HEARTBEAT_OK."

- name: weekly-health-review
  interval: 7d
  prompt: "Review the last 7 days of sessions, health logs, structured health files, biometrics, WHOOP summaries under health/whoop/, nutrition, training, supplements, and indexed media. Promote durable facts, patterns, and successful or failed experiments into MEMORY.md and the relevant health/*.md files. Append a concise dated weekly summary to health/reports/weekly-review.md. If there is no meaningful new signal, reply HEARTBEAT_OK."

# Additional instructions

- Prefer updating files over sending messages.
- Keep the review concise and practical.
- If there is no important issue that Dill needs to see immediately, reply HEARTBEAT_OK.
