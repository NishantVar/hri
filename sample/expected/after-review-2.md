---
spec_id: pomodoro-mvp
title: Pomodoro Timer MVP
---

# Pomodoro Timer MVP

A minimal Pomodoro timer for iOS. This spec is the demo fixture used by the RIView renderer and applier.

## Goals

<!-- node:deci-platform -->
**Platform: iOS-only for v1.**

Ship to iOS using SwiftUI on iOS 17+. Android, web, and macOS are explicitly out of scope until usage justifies the cost. Reviewed and confirmed 2026-05-23.
<!-- /node:deci-platform -->

## Sync model

<!-- node:amb-sync -->
**How should sessions sync across devices?**

The user may run the app on iPhone and iPad and expects an in-progress session to continue across devices.
<!-- /node:amb-sync -->

## Notifications

<!-- node:deci-notify -->
**Notifications: local-only.**

Use `UNUserNotificationCenter` for end-of-session alerts. No push backend required for v1 — every notification is scheduled on-device.
<!-- /node:deci-notify -->

## Risks

<!-- node:risk-bg -->
**iOS background timer limits.**

A 25-minute timer ticking in the background may be killed by iOS before it fires. Falling back to a pre-scheduled local notification at the deadline mitigates the user-visible failure mode.
<!-- /node:risk-bg -->

<!-- node:risk-watch -->
**Apple Watch parity expectation.**

Users may expect a Watch companion at launch. Lack of one could be perceived as a gap relative to existing competitors.
<!-- /node:risk-watch -->

## Open questions

<!-- node:amb-streaks -->
**Should streaks be a first-class feature or a stretch goal?**

Streaks are a strong retention hook but add data-model complexity (per-day session counts, gap handling, recovery flows).
<!-- /node:amb-streaks -->
