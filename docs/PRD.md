# PRD: FaceTag — AR Name Recall Glasses

> **Team:** 3 people | **Timeline:** Hackathon (1 hour) | **Hardware:** Rokid Glasses (standalone, WiFi direct)

---

## Problem Statement

Nobody remembers names after group introductions. You meet 10 people in 5 minutes and immediately forget 8 of them. It's universal, it's embarrassing, and there's no good solution.

## Solution

AR glasses that automatically learn who people are during introductions, then display their name and context as a HUD overlay whenever you see them again.

**The flow:**
1. Wear Rokid Glasses during an introduction session
2. Tap to enter "Learn mode" — glasses capture the person's face + what they say
3. Backend extracts face embedding + transcribes their introduction (name, role, fun fact)
4. Face → identity mapping stored in SQLite
5. In "Recall mode," glasses continuously stream video — when a known face appears, their name + role displays on the HUD
6. Swipe on glasses temple to see their fun fact (stretch goal)

---

## Architecture

Standalone glasses → server over WiFi. No phone required.

```
┌──────────────────────────────────┐
│         Rokid Glasses            │
│         (CXR-S Android app)      │
│                                  │
│  Camera2 → H.264 encoder ──┐    │
│  8ch mic → mono PCM/AAC ───┤    │
│                             │    │
│  WebSocket client ──────────┘    │
│       ▲                          │
│       │ JSON: {name, role}       │
│       │                          │
│  HUD overlay renderer            │
│  Gesture handler (tap/swipe)     │
└──────────────┬───────────────────┘
               │ WSS (zrok tunnel)
               ▼
┌──────────────────────────────────┐
│         Backend Server           │
│         (Python, WebSocket)      │
│                                  │
│  Stream handler:                 │
│   • 0x03 H.264 video frames     │
│   • 0x01 PCM audio / 0x04 AAC   │
│                                  │
│  Recognition pipeline:           │
│   • Decode H.264 → BGR frame     │
│   • face_recognition embeddings  │
│   • Match against SQLite DB      │
│   • Send JSON result back on WS  │
│                                  │
│  Enrollment pipeline:            │
│   • Triggered by "enroll" cmd    │
│   • Grab current frame → embed   │
│   • Grab recent audio → Whisper  │
│   • LLM extract name/role/fact   │
│   • Store in SQLite              │
│                                  │
│  DB: persons table               │
│   id, name, role, fun_fact,      │
│   embedding (blob), created_at   │
└──────────────────────────────────┘
```

### What Already Exists (built by teammate)

**Glasses app (Kotlin/Android):**
- Camera2 → H.264 640x480 @ 10fps → WebSocket (frame type 0x03)
- 8-channel Rokid mic → downmixed to mono → PCM 0x01 + AAC 0x04
- WebSocket client connecting via zrok tunnel
- Swipe gesture detection (forward/back via key events)
- Audio playback from server

**Server (Python):**
- WebSocket receiver for H.264 + audio streams
- Buffers and muxes into 10-second MP4 clips
- ffmpeg integration for audio/video processing

### What Needs to Be Built

**Server additions (Kaleb):**
- Face recognition pipeline inline on the WebSocket stream
- Person database (SQLite)
- Enrollment flow (triggered by glasses command)
- Audio transcription → name/role extraction
- Send recognition results back to glasses as JSON

**Glasses app additions (glasses teammate):**
- HUD overlay renderer for name + role text
- Tap gesture → send "enroll" command over WebSocket
- Parse incoming JSON recognition results
- Stretch: swipe to toggle between role and fun fact

**Designer:**
- HUD overlay visual design
- Learn mode vs Recall mode states
- Pitch deck / demo narrative

---

## WebSocket Protocol

All communication over a single persistent WebSocket connection.

### Glasses → Server (binary frames)

| Byte 0 | Payload | Description |
|---------|---------|-------------|
| 0x01 | PCM 16kHz mono 16-bit | Raw microphone audio |
| 0x03 | H.264 NAL units | Encoded video stream |
| 0x04 | AAC ADTS frames | Encoded audio stream |

### Glasses → Server (text frames)

```json
{"command": "enroll"}
{"command": "set_mode", "mode": "learn" | "recall"}
```

### Server → Glasses (text frames)

```json
{
  "type": "recognition",
  "matched": true,
  "person": {
    "name": "Sarah Chen",
    "role": "PM @ Microsoft",
    "fun_fact": "Plays guitar",
    "confidence": 0.87
  }
}

{
  "type": "recognition",
  "matched": false
}

{
  "type": "enrolled",
  "person": {
    "name": "Sarah Chen",
    "role": "PM @ Microsoft"
  }
}
```

---

## Recognition Pipeline (Server)

1. Receive H.264 frame (0x03) from WebSocket
2. Every 5th frame (~2 FPS effective): decode to BGR using OpenCV/ffmpeg
3. Run `face_recognition.face_encodings()` → 128-dim embedding
4. Compare against all stored embeddings using `face_recognition.face_distance()`
5. If best match distance < 0.6 → send recognition JSON back
6. If no match → send `{matched: false}` (or nothing, to reduce noise)
7. Target: < 500ms from frame receipt to response

## Enrollment Pipeline (Server)

1. Receive `{"command": "enroll"}` text frame from glasses
2. Grab the most recent decoded video frame → extract face embedding
3. Grab the last ~10 seconds of buffered audio → transcribe with Whisper
4. Extract name, role, fun_fact from transcription (regex or LLM prompt)
5. Store in SQLite: `(name, role, fun_fact, embedding, created_at)`
6. Send `{"type": "enrolled", "person": {...}}` back to glasses
7. If transcription fails, fall back: store embedding with `name="Unknown_N"`, allow manual edit later

---

## HUD Display Design

480x640 resolution, 30° FOV. Text must be large and high-contrast.

**Default view (Recall mode):**
```
┌────────────────────┐
│                    │
│                    │
│                    │
│  ┌──────────────┐  │
│  │ Sarah Chen   │  │
│  │ PM @ MSFT    │  │
│  └──────────────┘  │
└────────────────────┘
```

**After swipe (stretch):**
```
┌────────────────────┐
│                    │
│                    │
│                    │
│  ┌──────────────┐  │
│  │ Sarah Chen   │  │
│  │ 🎸 Plays     │  │
│  │    guitar    │  │
│  └──────────────┘  │
└────────────────────┘
```

- Name: large, bold, white on semi-transparent black
- Context: medium, below name
- Position: bottom-center of FOV
- Display duration: 5 seconds, then fade
- Only show when confidence > 0.6

**Learn mode indicator:**
```
┌────────────────────┐
│  ● LEARNING        │
│                    │
│                    │
│                    │
│                    │
└────────────────────┘
```

---

## Database Schema

```sql
CREATE TABLE persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    fun_fact TEXT,
    embedding BLOB NOT NULL,  -- 128 float32s, 512 bytes
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP
);
```

---

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Glasses app | Kotlin, Jetpack Compose, CXR-S SDK | Already built |
| Video codec | H.264 via MediaCodec | Already built, 640x480 @ 10fps |
| Audio codec | AAC (ADTS) + raw PCM | Already built |
| Transport | WebSocket (OkHttp ↔ websockets lib) | Already built |
| Tunnel | zrok | Already configured |
| Backend | Python, websockets, aiohttp | Partially built |
| Face detection | face_recognition (dlib) | pip install |
| Face embeddings | face_recognition (128-dim) | Same lib |
| STT | OpenAI Whisper API | Fast, accurate |
| Name extraction | GPT-4o-mini or regex | Parse intro text |
| Database | SQLite | Built-in, zero setup |
| Video decode | OpenCV or ffmpeg (subprocess) | For H.264 → BGR |

---

## MVP Scope (Hackathon)

### Must Have
- [ ] Server: decode H.264 frames inline from WebSocket stream
- [ ] Server: run face_recognition on decoded frames (~2 FPS)
- [ ] Server: SQLite persons table with embedding storage + matching
- [ ] Server: send recognition JSON back over WebSocket
- [ ] Server: enrollment triggered by glasses command (grab frame + store)
- [ ] Glasses: render name + role as HUD overlay from server JSON
- [ ] Glasses: tap to send enroll command

### Nice to Have
- [ ] Whisper transcription for auto-extracting name during enrollment
- [ ] LLM extraction of role + fun fact from transcription
- [ ] Swipe gesture to toggle between role and fun fact
- [ ] Learn mode vs Recall mode visual indicator on HUD
- [ ] Multiple faces in a single frame

### Backlog (Post-Hackathon)
- [ ] Speaker voiceprint as second biometric (pyannote/resemblyzer)
- [ ] Persistent cloud database
- [ ] Privacy controls / consent flow
- [ ] LinkedIn profile enrichment
- [ ] Multi-user support (multiple glasses wearers)
- [ ] On-device inference (skip the server)
- [ ] Offline mode with local embedding cache

---

## Team Split

### Person A — Glasses App (has hardware + ADB)
- Add HUD overlay renderer (Jetpack Compose text overlay)
- Add tap gesture → send `{"command": "enroll"}` over WebSocket
- Parse incoming JSON recognition results → display name + role
- Stretch: swipe gesture toggles fun fact view

### Person B — Designer
- HUD overlay visual design (mockup for 480x640)
- Learn mode vs Recall mode visual states
- Pitch deck / demo script
- Help with glasses UI if time permits

### Person C — Backend (Kaleb)
- Add face recognition to existing WebSocket handler
- Decode H.264 → BGR frames inline
- face_recognition for embeddings + matching
- SQLite persons table
- Enrollment flow (frame + optional audio → store)
- Send recognition JSON back to glasses
- Stretch: Whisper + LLM for auto-enrollment from audio

---

## Risks

| Risk | Mitigation |
|------|------------|
| H.264 decode performance | Only decode every 5th frame (~2 FPS) |
| face_recognition accuracy at 640x480 | Sufficient for close-range intros (1-3 meters) |
| WiFi/zrok latency | Already tested by teammate — working |
| Enrollment without audio transcription | Manual name entry as fallback |
| Rokid HUD text readability | Large font, high contrast, minimal text |

---

*Created: 2026-03-28 | Hackathon PRD | Team: 3*
