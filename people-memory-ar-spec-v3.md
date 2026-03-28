# People Memory — AR Experience Design Spec

**Platform:** Rokid Glasses (AI + AR, 49g model)
**Document type:** UX & Interaction Design Specification
**Author:** Spherex (Product & UX Design)
**Version:** 3.0 · March 2026

---

## 1. Experience Vision

People Memory runs silently in the background. The camera is always on, always watching. Every face you encounter is quietly captured and stored as an embedding. When you meet someone, you simply tell the glasses their name, role, and a fun fact. The next time you see that person — whether it's an hour later or six months later — their name, role, and fun fact appear on the HUD automatically.

No trigger. No "start recording." No mode to activate. It just works.

**Design principles:**

- **Always-on, zero-friction.** The system captures every face passively. The user never has to "start" anything — they only speak when they want to label someone.
- **Glance-down, not look-at.** The Rokid display lives in the bottom of your peripheral vision. Information is absorbed with a quick downward glance.
- **Green on dark.** The monochrome green waveguide display is the only visual channel. Hierarchy = brightness + size + position.
- **Graceful degradation.** If recognition confidence is low, show nothing rather than show wrong information.

---

## 2. Hardware Constraints (Design-Critical)

| Property | Rokid Glasses Spec | Design Implication |
|----------|-------------------|-------------------|
| **Display type** | Monochrome green, diffractive waveguide | No color. Hierarchy = brightness + size + position only |
| **Display position** | Bottom half of peripheral vision | User glances down to read. Never obscures faces. |
| **Field of view** | 23° (narrow) | Very limited canvas. Every character counts. |
| **Brightness** | Up to 1500 nits, 10-level dimming | Readable outdoors, must auto-dim indoors |
| **Display per eye** | Dual-eye (binocular) | Text is readable with depth presence |
| **Weight** | 49g | All-day wearable |
| **Camera** | 12MP Sony IMX681, F2.25, 77° H-FOV | Strong for face detection at 1–3m conversational distance |
| **Camera indicator** | LED flashes during camera use | Always-on camera means LED is always active — see Privacy section |
| **Compute** | Qualcomm AR1 (AI/imaging) + NXP RT600 (low-power) | Face detection on-device; embedding + matching offloaded to phone |
| **Audio output** | Dual HD directional speakers | Private chimes for the wearer only |
| **Input** | Touch strip (right temple) + voice ("Hi, Rokid") + physical button | Three input channels |
| **Connectivity** | Bluetooth to phone (Hi Rokid app) | Phone is the compute/storage backend |
| **Battery** | 210 mAh (4–6 hrs typical use) | Always-on camera = significant drain. Duty-cycling strategy critical. |

---

## 3. System Architecture

```
┌──────────────────┐  Bluetooth  ┌──────────────────────────────────┐
│  ROKID GLASSES    │◄──────────►│  PHONE (Hi Rokid App)             │
│                  │             │                                   │
│  ALWAYS RUNNING: │             │  PROCESSING:                      │
│  • Camera feed   │── frames ──►│  • Face detection + embedding     │
│    (duty-cycled) │             │  • Unknown face → auto-store      │
│                  │             │  • Known face → retrieve record   │
│  DISPLAY:        │             │                                   │
│  • HUD overlay   │◄── text ────│  STORAGE:                         │
│                  │             │  • People DB (SQLite + FAISS)     │
│  INPUT:          │             │  • Embeddings, names, roles, facts│
│  • Microphone    │── audio ───►│  • Speech-to-text + NLP           │
│  • Touch strip   │── input ───►│  • Intent parsing                 │
│  • Speakers      │◄── chimes ──│                                   │
└──────────────────┘             └──────────────────────────────────┘
```

### 3.1 — Always-On Pipeline

The camera runs continuously (duty-cycled for battery). Every detected face flows through this pipeline:

```
Camera frame
    │
    ▼
Face detected? ──── No ───► discard frame
    │
   Yes
    │
    ▼
Generate embedding
    │
    ▼
Match against database
    │
    ├── KNOWN PERSON (>70% confidence)
    │       │
    │       ▼
    │   Display name + role + fun fact on HUD
    │
    ├── UNKNOWN PERSON (new face, no match)
    │       │
    │       ▼
    │   Auto-store embedding silently
    │   Tag as "unlabeled" in database
    │   Wait for user to provide name/role/fact
    │
    └── LOW CONFIDENCE (ambiguous)
            │
            ▼
        Show nothing. Do not guess.
```

**Key behavior:** Unknown faces are stored automatically as unlabeled entries. The user doesn't need to do anything. Later, when the user says a name/role/fact, the system attaches that metadata to the most recently detected unknown face.

---

## 4. Interaction Modes

### 4.1 — PASSIVE CAPTURE (Always Running)

The camera is always on. There is no "capture mode" to trigger.

**What happens automatically (no user action):**

- Every face in frame is detected and embedded
- New faces are stored as unlabeled entries with timestamp and face crop
- Known faces trigger the HUD overlay immediately
- All of this is invisible to the user — no indicators, no prompts

**What the user does (only when they want to label someone):**

The user simply speaks the person's info naturally. The system attaches it to the most recent new face detected.

```
"Her name is Sarah Chen"
"She's a product manager at Google"
"Fun fact: she ran a marathon in Antarctica"
```

Or as a single natural sentence:

```
"That's Sarah Chen, product manager at Google — she ran a marathon in Antarctica"
```

Or even just a name to start:

```
"That's Sarah"
```

The role and fun fact can be added later — in the moment, after a conversation, or from the companion app.

### 4.2 — LABELING FLOW (Attaching Info to a Face)

The system must intelligently match voice input to the right face.

| Scenario | How labeling works |
|----------|-------------------|
| **One unknown face in frame** | Voice input automatically attaches to that face |
| **Multiple unknown faces in frame** | System attaches to the face the user is most directly looking at (center of camera frame) |
| **No face currently in frame** | System attaches to the most recently detected unknown face (within last 60 seconds) |
| **User says a name that matches an existing unlabeled entry** | Shouldn't happen — system uses face proximity, not name matching, to assign labels |

**Labeling confirmation on HUD:**

```
┌─────────────────────────────────────────┐
│                                         │
│  ✓ Sarah Chen                           │
│    PM · Google                          │
│    Marathon in Antarctica               │
│                                         │
└─────────────────────────────────────────┘
        ▲ Holds for 2 seconds, then fades
```

### 4.3 — RECALL MODE (Re-encounter)

**Trigger:** Automatic. A known face enters the camera frame.

The HUD displays the person's info immediately. No action required from the user.

| Condition | HUD behavior |
|-----------|-------------|
| **High confidence (>90%)** | Show name + role + fun fact |
| **Medium confidence (70–90%)** | Show name + role only, with `?` |
| **Low confidence (<70%)** | Show nothing |
| **Multiple known faces** | Stack up to 2 people, closest first |
| **Face leaves frame** | Overlay fades over 0.5s |
| **Unlabeled known face** | Show nothing (face is stored but has no metadata yet) |

### 4.4 — EDIT MODE

User can update any field at any time by speaking:

```
"Hi Rokid, update Sarah Chen"
→ HUD shows current record:
  Sarah Chen | PM · Google | Marathon in Antarctica

"Change her role to Director of Product"
→ HUD: Director of Product ✓

"Add fun fact: she's moving to London next month"
→ HUD: Moving to London ✓

"Save"
```

Or from the companion app — edit any field with full keyboard.

---

## 5. HUD Display Design

### 5.1 — Display Zone Map

```
   ┌─────────────────────────────────────────┐
   │                                         │
   │          REAL WORLD VIEW                │
   │          (person's face is here)         │
   │                                         │
   │                                         │
   ├═════════════════════════════════════════╡
   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│ ← HUD display zone
   │░░░  ROKID 23° FOV DISPLAY AREA  ░░░░░░│    (bottom peripheral)
   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│
   └─────────────────────────────────────────┘

   Straight-ahead gaze = person's face, unobstructed.
   Glance down = info card, like subtitles.
```

### 5.2 — Information Card Layout (3 Lines)

With name, role, AND fun fact, we now use 3 lines maximum per person. This is tight within 23° FOV but workable if each line is compact.

**Single-person recall (full record):**

```
┌─────────────────────────────────────────┐
│                                         │
│  ● Sarah Chen                           │  ← Name: 100% bright, bold
│    PM · Google                          │  ← Role: 70% bright, regular
│    Marathon in Antarctica               │  ← Fact: 50% bright, regular
│                                         │
└─────────────────────────────────────────┘
```

**Single-person recall (partial record — no fun fact yet):**

```
┌─────────────────────────────────────────┐
│                                         │
│  ● Sarah Chen                           │
│    PM · Google                          │
│                                         │
│                                         │
└─────────────────────────────────────────┘
```

**Single-person recall (name only — role and fact not yet added):**

```
┌─────────────────────────────────────────┐
│                                         │
│  ● Sarah Chen                           │
│                                         │
│                                         │
│                                         │
└─────────────────────────────────────────┘
```

**Multi-person recall (max 2 people):**

```
┌─────────────────────────────────────────┐
│  ● Sarah Chen                           │
│    PM · Google                          │
│    Marathon in Antarctica               │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─       │
│  ○ Dev Patel                            │
│    Engineer · Meta                      │
└─────────────────────────────────────────┘
```

**Note:** When two people are displayed, the secondary person shows name + role only (no fun fact) to save vertical space. Fun fact is reserved for the primary (closest) person.

### 5.3 — Monochrome Green Typography System

Three-tier hierarchy using brightness alone:

| Element | Brightness | Size | Weight | Purpose |
|---------|-----------|------|--------|---------|
| **Name** | 100% (full bright green) | Large | Bold | Identity — must register in 0.3s |
| **Role** | 70% (medium green) | Medium | Regular | Professional context — scanned second |
| **Fun fact** | 50% (dim green) | Small | Regular | Personal detail — scanned third |
| **Confidence dot** | ●100% high / ○40% pulsing medium | 6px | — | Trust signal |
| **Status text** (✓, Saved) | 80% | Small | Regular | Transient feedback |
| **Separator** | 25% | 1px dashed | — | Between people in multi-view |

**Information hierarchy is deliberate:** Name is always the loudest. Role sits in the middle — it's the most useful context for professional settings. Fun fact is the quietest — it's the personal touch, meant to be discovered on a closer glance.

### 5.4 — Text Formatting Rules

**Role formatting:** Title `·` Company — always this pattern.

| Spoken input | HUD display |
|-------------|------------|
| "She's a product manager at Google" | `PM · Google` |
| "He runs engineering at a startup called Luma" | `Engineering · Luma` |
| "She's a freelance photographer" | `Freelance photographer` |
| "He's in finance" | `Finance` |

**Fun fact formatting:** Same truncation rules — max ~35 characters on HUD, full text in companion app.

| Spoken input | HUD display |
|-------------|------------|
| "She ran a marathon in Antarctica last December" | `Marathon in Antarctica` |
| "He's building an AI that writes music" | `Builds AI music tools` |
| "They have three golden retrievers named after Greek gods" | `3 golden retrievers · Greek gods` |

**NLP requirement:** The system needs to parse natural speech into structured fields:

```
Input:  "That's Sarah Chen, she's a PM at Google, fun fact she ran 
         a marathon in Antarctica"

Parsed: {
          name: "Sarah Chen",
          role: { title: "PM", company: "Google" },
          fun_fact: "ran a marathon in Antarctica"
        }
```

### 5.5 — Animation & Timing

| Event | Animation | Duration |
|-------|-----------|----------|
| **Known face enters frame** | Fade in from 0% to target brightness | 400ms, ease-out |
| **Face leaves frame** | Fade to 0% | 600ms, ease-in |
| **Label confirmed** | All text flashes 100% once, then settles | 300ms |
| **"Saved" feedback** | Appears → holds → fades | 2s total |
| **Idle timeout** | Dims to 30% after 10s, fades at 15s | Prevents visual fatigue |

**No slide/motion animations.** Brightness transitions only — waveguide displays handle fades cleanly but motion creates artifacts.

---

## 6. Voice & Touch Interface

### 6.1 — Voice Commands

There is no "capture mode" to enter. The user just speaks when they want to label or update.

**Labeling (new person):**

| What the user says | What happens |
|-------------------|-------------|
| `"That's [Name]"` | Attaches name to most recent/current unknown face |
| `"[Name], [role] at [company]"` | Attaches name + role |
| `"[Name], [role] at [company], fun fact: [fact]"` | Attaches all three fields |
| `"Her name is [Name]"` | Attaches name |
| `"He's a [role] at [company]"` | Attaches role to the current face (must already have name, or adds to most recent) |
| `"Fun fact: [fact]"` | Attaches fun fact to current/most recent face |

**The system should be forgiving about sentence structure.** People won't use a rigid format in conversation. The NLP layer should handle natural variations:

```
"That's Sarah, she does product at Google"
"Sarah Chen — PM, Google — she ran a marathon in Antarctica"  
"Her name is Sarah, works at Google"
"Oh that's Dev, he's an engineer"
```

**Editing (existing person):**

| Command | Action |
|---------|--------|
| `"Hi Rokid, update [Name]"` | Enter Edit Mode for that person |
| `"Change role to [new role]"` | Update role |
| `"Add fun fact: [fact]"` | Append new fun fact |
| `"Change fun fact to [fact]"` | Replace fun fact |
| `"Hi Rokid, forget [Name]"` | Delete entry (requires voice confirmation: "Are you sure?") |

**Querying:**

| Command | Action |
|---------|--------|
| `"Hi Rokid, who is this?"` | Force recall attempt on current face |
| `"Hi Rokid, tell me about [Name]"` | Speaks name + role + fact through directional speakers |

### 6.2 — Touch Strip Gestures

| Gesture | Action |
|---------|--------|
| **Single-tap** | Pin/unpin current HUD card (prevent auto-dismiss) |
| **Double-tap** | Dismiss current card immediately |
| **Swipe forward** | In multi-person view: cycle to next person's full details |
| **Swipe back** | Dismiss and suppress card for this encounter |
| **Long-press (1.5s)** | Trigger voice input mode (alternative to "Hi Rokid" wake word) |

### 6.3 — Audio Feedback

| Event | Sound | Duration |
|-------|-------|----------|
| Label saved | Double soft chime | 0.3s |
| Error / voice not understood | Single low tone | 0.2s |
| Person recognized (optional — off by default) | Single subtle tick | 0.1s |
| Delete confirmed | Descending two-tone | 0.3s |

**Recognition chime is OFF by default** — in a room full of known people, constant chimes would be maddening. User can enable in settings.

---

## 7. Always-On Camera: Battery & Performance Strategy

The always-on camera is the biggest technical challenge. This section is critical for developers.

### 7.1 — Duty-Cycling Strategy

The camera does NOT need to run at 30fps continuously. Face detection in social contexts is tolerant of latency.

| Mode | Camera behavior | Scan interval | Battery impact |
|------|----------------|---------------|---------------|
| **Active social** (faces detected recently) | Higher duty cycle | Every 1–2 seconds | Moderate |
| **Idle** (no faces detected for 30s) | Low duty cycle | Every 5 seconds | Low |
| **Sleep** (glasses stationary / no motion detected) | Camera off | — | Minimal |
| **Battery saver** (<15% battery) | Camera off | — | None |

### 7.2 — Frame Processing Budget

Per scan cycle:

| Step | Target time | Where it runs |
|------|------------|---------------|
| Camera capture (single frame) | <50ms | Glasses (AR1 chip) |
| Face detection | <100ms | Glasses (AR1) or phone |
| Face embedding (512-dim) | <150ms | Phone (GPU) |
| FAISS vector search | <10ms | Phone |
| BLE round-trip (frame → result) | <200ms | Bluetooth |
| **Total end-to-end** | **<500ms** | — |

### 7.3 — Battery Life Targets

| Usage scenario | Target battery life |
|---------------|-------------------|
| Always-on, active social (networking event) | 2.5–3 hours |
| Mixed use (some social, some idle) | 4 hours |
| Low duty-cycle (occasional encounters) | 5+ hours |

Developers should benchmark against baseline Rokid battery life (4–6 hrs) and target no more than 30% additional drain in active social mode.

---

## 8. Privacy & Ethics Design

### 8.1 — The Always-On Camera Problem

An always-on face-scanning camera is the most sensitive aspect of this product. This section is non-negotiable.

**The core tension:** The product's value comes from passive, effortless capture. But passive face scanning is exactly what makes people uncomfortable. The design must earn trust.

### 8.2 — Privacy Rules

1. **No raw images stored.** The camera captures frames for face detection only. Frames are processed in memory and discarded. Only the computed face embedding (a mathematical vector, not a photo) is stored. Exception: a small face-crop thumbnail is stored for the companion app gallery — this must be clearly disclosed.
2. **Camera indicator LED is always on.** The Rokid hardware already has a camera indicator light. Since People Memory runs the camera continuously, this LED will be on/flashing whenever the glasses are active. This is a feature, not a bug — it signals to others that the camera is active.
3. **All data on-device / user's phone.** No cloud. No server. No sync unless the user explicitly enables it.
4. **Unlabeled faces auto-expire.** Unknown faces that are never labeled should be automatically deleted after a configurable period (default: 7 days). This prevents the system from accumulating a database of strangers.
5. **Easy bulk delete.** One-tap "delete all" in the companion app.
6. **Pause mode.** User can pause all face capture via touch gesture (e.g., triple-tap) or voice ("Hi Rokid, pause memory"). HUD shows `Memory paused` briefly. Resume with same gesture/command.

### 8.3 — Consent Considerations

| Context | Recommendation |
|---------|---------------|
| **1-on-1 conversation** | The camera LED provides a passive signal. Socially, the user should mention they're using smart glasses if asked. |
| **Group / networking event** | Acceptable — glasses are visible and LED is active. Consider event-level disclosure (e.g., "I'm wearing smart glasses that help me remember names"). |
| **Private spaces** | User should activate Pause mode in medical offices, restrooms, schools, etc. |
| **Jurisdictions with strict biometric laws (BIPA, GDPR)** | The companion app must include a jurisdiction-aware consent flow. In BIPA states, the app should warn the user and may require them to obtain verbal consent before labeling someone. |

### 8.4 — Auto-Expiry for Unlabeled Faces

This is an important privacy safeguard. Without it, the system becomes a surveillance tool.

| Face status | Retention policy |
|-------------|-----------------|
| **Labeled** (has name) | Kept indefinitely until user deletes |
| **Unlabeled** (detected but never labeled) | Auto-deleted after 7 days (configurable: 1 day / 7 days / 30 days / never) |
| **All data** | User can export or delete all at any time |

---

## 9. Data Schema

```json
{
  "person_id": "uuid-v4",
  "face_embedding": [0.023, -0.118, ...],     // 512-dim vector
  "status": "labeled",                         // "labeled" | "unlabeled"
  
  "name": "Sarah Chen",
  
  "role": {
    "title": "Product Manager",
    "title_short": "PM",                       // for HUD display
    "company": "Google"
  },
  
  "fun_facts": [
    {
      "text_full": "She ran a marathon in Antarctica last December",
      "text_hud": "Marathon in Antarctica",    // ≤35 chars for HUD
      "added_at": "2026-03-28T14:30:00Z"
    }
  ],
  
  "first_seen": "2026-03-28T14:30:00Z",
  "last_seen": "2026-04-15T09:12:00Z",
  "times_recognized": 4,
  "thumbnail": "base64-encoded-crop",          // small face crop for app
  "confidence_history": [0.94, 0.91, 0.88, 0.96],
  "auto_expire_at": null                       // set for unlabeled faces only
}
```

---

## 10. Edge Cases & Error States

| Scenario | Behavior |
|----------|----------|
| **New face, user says nothing** | Face stored as unlabeled. No HUD display. Auto-expires after 7 days. |
| **User labels someone who already left the frame** | System attaches label to most recently detected unknown face (within 60s). |
| **User provides name only, no role/fact** | Stored with name only. Role and fact can be added anytime later. HUD shows name only on recall. |
| **Two unknown faces in frame, user says one name** | Attach to the face closest to center of camera frame (assumed gaze direction). |
| **User provides role/fact without a name first** | System prompts on HUD: `Name?` — or stores as partial and lets user complete in companion app. |
| **Same person detected multiple times before labeling** | System deduplicates — face embeddings are merged if similarity >95%. |
| **Person changes appearance** | Medium-confidence match → show name with `?` → user confirms verbally: "Yes, that's Sarah." Embedding updated. |
| **Very crowded environment (10+ faces)** | System processes all but only stores embeddings for faces within 3m. Distant faces are discarded. |
| **User looks in mirror** | User's own face excluded (calibrated during onboarding). |
| **Battery < 15%** | Camera off. HUD: `Low battery · Memory paused`. |
| **Bluetooth disconnected** | If lightweight on-device model exists: name-only recall. Otherwise: HUD shows `Phone disconnected`. |
| **User says wrong info** | "Hi Rokid, update Sarah — change name to Sara" — edit flow handles corrections. |
| **Rapid-fire networking (many new faces in 5 min)** | All faces auto-stored. User can label in batch later via companion app. |

---

## 11. Onboarding Flow

| Step | HUD shows | What happens | Duration |
|------|-----------|-------------|----------|
| 1. Self-enrollment | `Look straight ahead` | Captures user's face to exclude from future matching | 5s |
| 2. Explain always-on | `Camera is always watching for faces` | User understands the passive nature | 3s |
| 3. Demo recall | Sample card: name + role + fact | User sees what a re-encounter looks like | 5s |
| 4. Practice labeling | `Try it: say someone's name nearby` | User labels a real person for the first time | 20s |
| 5. Privacy notice | `All data stays on your phone` | Privacy policy summary | 3s |
| 6. Pause gesture | `Triple-tap to pause anytime` | User practices the pause gesture | 5s |

**Total onboarding target:** Under 45 seconds.

---

## 12. Companion App (Hi Rokid Integration)

**Screens needed:**

- **People Gallery** — Grid of face thumbnails + names. Filter: labeled / unlabeled / all. Unlabeled faces show as `Unknown · March 28`. Tap to label or delete.
- **Person Detail** — Name, role (full title + company), all fun facts with timestamps, recognition count, first/last seen. Edit all fields. Delete person.
- **Unlabeled Queue** — List of unlabeled face entries with thumbnails and timestamps. User can label from here after an event (e.g., "Oh, that was the guy from the coffee line — his name was Marcus"). 
- **Event Mode** — Toggle for conferences/parties. Increases scan frequency. After event ends, shows a summary: "You met 12 new people. 5 labeled, 7 unlabeled." Prompts user to label the rest.
- **Settings** — Auto-expire duration for unlabeled faces, recognition confidence threshold, HUD idle timeout, brightness, battery saver toggle, data export, delete all.
- **Privacy Dashboard** — Shows: total faces stored, total labeled vs unlabeled, storage used, last auto-expire purge date.

---

## 13. Success Metrics

| Metric | Target | How to measure |
|--------|--------|---------------|
| Face capture rate (faces in frame that get embedded) | >95% | Lab test at various distances and angles |
| Recognition accuracy (known labeled person) | >90% true positive | Field test across diverse faces |
| False positive rate | <2% | Field test |
| Labeling success (voice → correct field assignment) | >90% | User testing with natural speech |
| HUD readability (3-line card) | >80% "easy to read at a glance" | User testing (n=10+) |
| Time-to-recognition (re-encounter) | <2 seconds | Including duty-cycle interval |
| Battery life in active social mode | >2.5 hours | Bench test |
| Onboarding completion | >95% | Analytics |
| Unlabeled face auto-expire compliance | 100% | Unit test |

---

## Appendix A — Recommended Tech Stack

| Layer | Recommendation | Why |
|-------|---------------|-----|
| **Face detection** | MediaPipe Face Detection | Lightweight, runs on AR1 chip |
| **Face embedding** | ArcFace (MobileFaceNet variant) | Good accuracy at 512-dim, mobile-optimized |
| **Vector search** | FAISS (IVF-Flat) on phone | Sub-ms nearest-neighbor at 1K+ faces |
| **Speech-to-text** | Rokid's built-in STT or Whisper (small) | Leverage existing "Hi, Rokid" pipeline |
| **NLP parsing** | LLM-based intent parser (lightweight) | Natural speech → structured fields requires more than regex |
| **Local DB** | SQLite on phone | Reliable, backup-friendly |
| **HUD rendering** | Rokid SDK display API | Must confirm: custom text with variable brightness? |
| **Phone ↔ Glasses** | Rokid BLE protocol | Must confirm: frame streaming latency |

**Note on NLP:** v2 suggested regex for parsing. With the always-on model and freeform labeling ("That's Sarah, she does product at Google, oh and she ran a marathon in Antarctica"), regex won't cut it. A lightweight LLM or fine-tuned NER model is needed to reliably extract name, role/title, company, and fun fact from natural conversational speech.

---

## Appendix B — Open Questions for Dev Feasibility

| # | Question | Affects |
|---|----------|---------|
| 1 | Can the Rokid SDK render custom text overlays with variable brightness levels? | Entire HUD typography system |
| 2 | What is the BLE latency for streaming camera frames to the phone? | Always-on pipeline viability |
| 3 | Can the AR1 chip run face detection locally, sending only detected face crops to the phone? | Battery + bandwidth optimization |
| 4 | What is the actual pixel resolution of the 23° waveguide display? | Text sizing, 3-line card feasibility |
| 5 | Can the camera run in a low-power duty-cycle mode via SDK? | Battery strategy |
| 6 | Does the camera indicator LED flash continuously during duty-cycled capture? | Privacy signaling |
| 7 | Can touch strip gestures be programmatically customized? | Gesture input design |
| 8 | Does the Hi Rokid app support third-party feature plugins or is a separate app needed? | Companion app strategy |
| 9 | What is the max BLE bandwidth? Can we stream 1 frame/sec at reduced resolution? | Processing architecture |
| 10 | Can the glasses trigger the phone to process a frame without waking the phone screen? | Background processing |

---

## Appendix C — Comparison: v2 (Triggered Capture) vs v3 (Always-On)

| Dimension | v2 (Triggered) | v3 (Always-On) |
|-----------|---------------|-----------------|
| **Capture initiation** | User says "Remember this person" | Automatic — camera always running |
| **Friction** | Medium — requires conscious trigger | Zero — user only speaks to label |
| **Battery impact** | Lower — camera active only when triggered | Higher — always-on duty-cycling |
| **Privacy risk** | Lower — camera on only during capture | Higher — continuous face scanning |
| **Unlabeled faces** | Not stored | Stored temporarily, auto-expired |
| **User mental model** | "I choose who to remember" | "It sees everyone; I choose who to name" |
| **Best for** | Privacy-sensitive users | Networking-heavy, social professionals |

---

*End of design specification. v3.0 — always-on passive capture, role field added, no trigger required.*
