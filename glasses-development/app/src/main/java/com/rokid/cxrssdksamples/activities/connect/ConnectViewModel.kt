package com.rokid.cxrssdksamples.activities.connect

import android.app.Application
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.util.Log
import android.os.Handler
import android.os.HandlerThread
import android.view.Surface
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import com.rokid.cxrssdksamples.default.CONSTANT
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

enum class ConnectionState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
}

/** Binary frame types for WebSocket. */
private const val FRAME_TYPE_AUDIO: Byte = 0x01       // PCM audio (for assistant/Gemini mode)
private const val FRAME_TYPE_IMAGE: Byte = 0x02       // JPEG image
private const val FRAME_TYPE_VIDEO_H264: Byte = 0x03  // H.264/AVC video
private const val FRAME_TYPE_AUDIO_AAC: Byte = 0x04   // AAC-encoded audio (for stream-only recording)
private const val FRAME_TYPE_AUDIO_POST_ALGORITHM: Byte = 0x05  // Post-algorithm PCM (ch 0/1)

class ConnectViewModel(application: Application) : AndroidViewModel(application) {

    private val TAG = "ConnectViewModel"

    // ---------------------------------------------------------------------------
    // Public state observed by the UI
    // ---------------------------------------------------------------------------

    private val _connectionState = MutableStateFlow(ConnectionState.DISCONNECTED)
    val connectionState = _connectionState.asStateFlow()

    private val _statusMessage = MutableStateFlow(
        "Ensure device has Wi‑Fi, then press Connect. All audio and video go to the server only (standalone)."
    )
    val statusMessage = _statusMessage.asStateFlow()

    private val _serverMessage = MutableStateFlow("")
    val serverMessage = _serverMessage.asStateFlow()

    // ---------------------------------------------------------------------------
    // Audio constants — 16 kHz, 16-bit PCM, mono (matches Google Live API input)
    // ---------------------------------------------------------------------------

    companion object {
        private const val SERVER_URL = "wss://rokidglasses.share.zrok.io"

        private const val MIC_SAMPLE_RATE = 16000
        private const val MIC_CHANNEL = CONSTANT.AUDIO_CHANNEL  // Rokid 8-channel mask
        private const val MIC_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private const val MIC_BUFFER = 1024
        private const val ROKID_TOTAL_CHANNELS = 8
        // Channels 0/1 = post-algorithm audio (noise-suppressed)
        // Channels 2/3/4/5 = raw 4-mic array, 6/7 = echo reference

        // Google Live API returns 24 kHz PCM audio
        private const val PLAYBACK_SAMPLE_RATE = 24000
        private const val PLAYBACK_CHANNEL = AudioFormat.CHANNEL_OUT_MONO
        private const val PLAYBACK_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        // HTTP upload endpoint for 5-second clips. Derived from SERVER_URL by
        // switching to HTTPS/HTTP and appending /upload_clip. The Python server
        // listens on UPLOAD_PORT; expose that via zrok as HTTPS.
        private val CLIP_UPLOAD_URL: String = run {
            val base = SERVER_URL
                .replaceFirst("wss://", "https://")
                .replaceFirst("ws://", "http://")
            base.trimEnd('/') + "/upload_clip"
        }
    }

    // ---------------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------------

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // no timeout on WebSocket reads
        .build()

    private var webSocket: WebSocket? = null
    private val webSocketRef = AtomicReference<WebSocket?>(null)
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null
    private var micJob: Job? = null
    private var aacEncoder: MediaCodec? = null
    private var aacDrainJob: Job? = null
    // Video encoder / camera2 state for HEVC streaming over WebSocket.
    private var cameraManager: CameraManager? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var encoder: MediaCodec? = null
    private var encoderSurface: Surface? = null
    private var encoderJob: Job? = null

    // ---------------------------------------------------------------------------
    // Connect / disconnect
    // ---------------------------------------------------------------------------

    fun connect() {
        if (_connectionState.value != ConnectionState.DISCONNECTED) return
        _connectionState.value = ConnectionState.CONNECTING
        _statusMessage.value = "Connecting to server…"

        // Quick connectivity probe to distinguish "no internet" from
        // "cannot reach WebSocket host". This runs in the background and
        // logs the result.
        logInternetConnectivity()

        val request = Request.Builder().url(SERVER_URL).build()
        webSocket = httpClient.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket connected")
                _connectionState.value = ConnectionState.CONNECTED
                _statusMessage.value = "Connected"
                webSocketRef.set(ws)
                startMicrophone(ws)
                startHevcVideoStream()
            }

            override fun onMessage(ws: WebSocket, bytes: ByteString) {
                val data = bytes.toByteArray()
                if (data.isEmpty()) return
                when (data[0]) {
                    FRAME_TYPE_AUDIO -> this@ConnectViewModel.playAudio(data.copyOfRange(1, data.size))
                    FRAME_TYPE_IMAGE -> { /* optional: show on UI */ }
                }
            }

            override fun onMessage(ws: WebSocket, text: String) {
                // Text frame = JSON from the server
                Log.i(TAG, "Server JSON: $text")
                try {
                    val json = org.json.JSONObject(text)
                    if (json.has("message")) {
                        _serverMessage.value = json.getString("message")
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to parse JSON: ${e.message}")
                }
                _statusMessage.value = "Connected • receiving data"
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closing: $code $reason")
                ws.close(1000, null)
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closed")
                teardown()
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "WebSocket failure: ${t.message}")
                _statusMessage.value = "Connection failed: ${t.message}"
                teardown()
            }
        })
    }

    fun logInternetConnectivity() {
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val request = Request.Builder()
                    .url("https://www.google.com/generate_204")
                    .build()
                val response = httpClient.newCall(request).execute()
                response.use {
                    if (it.isSuccessful) {
                        Log.i(TAG, "Internet check OK: HTTP ${it.code}")
                        _statusMessage.value = "Internet OK • Ready to connect"
                    } else {
                        Log.w(TAG, "Internet check failed: HTTP ${it.code}")
                        _statusMessage.value = "Internet check failed: HTTP ${it.code}"
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Internet check error: ${e.message}")
                _statusMessage.value = "Internet check error: ${e.message}"
            }
        }
    }

    fun disconnect() {
        webSocket?.close(1000, "User disconnected")
        webSocket = null
        teardown()
    }

    // ---------------------------------------------------------------------------
    // Microphone capture → WebSocket
    // ---------------------------------------------------------------------------

    /**
     * Start Camera2 + MediaCodec HEVC pipeline and stream encoded video as 0x03 over WebSocket.
     * This does not rely on CXR/Bluetooth.
     */
    private fun startHevcVideoStream() {
        val ctx = getApplication<Application>()
        try {
            cameraManager = ctx.getSystemService(CameraManager::class.java)
            val manager = cameraManager ?: return
            val cameraId = manager.cameraIdList.firstOrNull() ?: run {
                Log.e(TAG, "No camera available for HEVC stream")
                return
            }

            // Create background thread/handler for camera operations.
            cameraThread = HandlerThread("HevcCameraThread").apply { start() }
            cameraHandler = Handler(cameraThread!!.looper)

            // Configure encoder for H.264 640x480 @ ~10fps
            encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).apply {
                val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, 640, 480).apply {
                    setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                    setInteger(MediaFormat.KEY_BIT_RATE, 2_000_000)
                    setInteger(MediaFormat.KEY_FRAME_RATE, 10)
                    setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
                }
                configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
            encoderSurface = encoder?.createInputSurface()
            encoder?.start()

            manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    cameraDevice = camera
                    val surface = encoderSurface
                    if (surface == null) {
                        Log.e(TAG, "Encoder surface is null")
                        camera.close()
                        return
                    }
                    val requestBuilder = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                        addTarget(surface)
                        set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                    }
                    camera.createCaptureSession(
                        listOf(surface),
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(session: CameraCaptureSession) {
                                captureSession = session
                                try {
                                    session.setRepeatingRequest(requestBuilder.build(), null, null)
                                    Log.i(TAG, "Camera2 HEVC capture session started")
                                } catch (e: Exception) {
                                    Log.e(TAG, "Failed to start repeating request", e)
                                }
                            }

                            override fun onConfigureFailed(session: CameraCaptureSession) {
                                Log.e(TAG, "Camera2 configure failed for HEVC stream")
                            }
                        },
                        null
                    )
                }

                override fun onDisconnected(camera: CameraDevice) {
                    Log.w(TAG, "Camera disconnected")
                    cameraDevice = null
                }

                override fun onError(camera: CameraDevice, error: Int) {
                    Log.e(TAG, "Camera error: $error")
                    cameraDevice = null
                    camera.close()
                }
            }, cameraHandler)

            // Drain encoder output in background and send over WebSocket.
            encoderJob = viewModelScope.launch(Dispatchers.IO) {
                val codec = encoder ?: return@launch
                val bufferInfo = MediaCodec.BufferInfo()
                try {
                    while (_connectionState.value == ConnectionState.CONNECTED) {
                        val index = try {
                            codec.dequeueOutputBuffer(bufferInfo, 10_000)
                        } catch (ise: IllegalStateException) {
                            // Codec was stopped/released (e.g. teardown), exit cleanly.
                            break
                        }
                        if (index >= 0) {
                            val outBuffer = codec.getOutputBuffer(index)
                            if (outBuffer != null && bufferInfo.size > 0) {
                                val bytes = ByteArray(bufferInfo.size)
                                outBuffer.position(bufferInfo.offset)
                                outBuffer.limit(bufferInfo.offset + bufferInfo.size)
                                outBuffer.get(bytes)
                                val frame = ByteArray(bytes.size + 1)
                                frame[0] = FRAME_TYPE_VIDEO_H264
                                System.arraycopy(bytes, 0, frame, 1, bytes.size)
                                webSocketRef.get()?.send(frame.toByteString())
                            }
                            codec.releaseOutputBuffer(index, false)
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Encoder drain loop ended: ${e.message}")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start HEVC video stream", e)
        }
    }

    /**
     * Start Rokid 8-channel mic capture, extract post-algorithm channels 0/1,
     * downmix to mono, encode to AAC, and send as 0x04 frames over WebSocket.
     */
    private fun startMicrophone(ws: WebSocket) {
        // Rokid custom 8-ch mask: getMinBufferSize may return -1, so don't pass bufferSize.
        audioRecord = AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.MIC)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(MIC_SAMPLE_RATE)
                    .setChannelMask(MIC_CHANNEL)
                    .setEncoding(MIC_FORMAT)
                    .build()
            )
            .build()

        val aacFormat = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, MIC_SAMPLE_RATE, 1
        ).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, 64_000)
        }
        aacEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).apply {
            configure(aacFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            start()
        }
        val codec = aacEncoder ?: return

        audioRecord?.startRecording()

        // Read 8-channel PCM. Two streams:
        // - Raw: channels 2/3/4/5 (4 mics) → mono → 0x01 + AAC 0x04
        // - Post-algorithm: channels 0/1 (noise-suppressed) → mono → 0x05
        micJob = viewModelScope.launch(Dispatchers.IO) {
            val buffer = ShortArray(MIC_BUFFER * ROKID_TOTAL_CHANNELS)
            val rec = audioRecord ?: return@launch
            while (_connectionState.value == ConnectionState.CONNECTED) {
                val shortsRead = rec.read(buffer, 0, buffer.size)
                if (shortsRead <= 0) continue
                val framesRead = shortsRead / ROKID_TOTAL_CHANNELS

                val rawMono = ByteArray(framesRead * 2)   // raw: ch2,3,4,5 → mono
                val postAlgMono = ByteArray(framesRead * 2) // post-algorithm: ch0,1 → mono

                for (i in 0 until framesRead) {
                    val baseIdx = i * ROKID_TOTAL_CHANNELS
                    
                    // Post-algorithm (channels 0/1)
                    val ch0 = buffer[baseIdx].toInt()
                    val ch1 = buffer[baseIdx + 1].toInt()
                    val postMixed = ((ch0 + ch1) / 2).toShort()
                    postAlgMono[i * 2] = (postMixed.toInt() and 0xFF).toByte()
                    postAlgMono[i * 2 + 1] = (postMixed.toInt() shr 8 and 0xFF).toByte()
                    
                    // Raw (channels 2,3,4,5)
                    val ch2 = buffer[baseIdx + 2].toInt()
                    val ch3 = buffer[baseIdx + 3].toInt()
                    val ch4 = buffer[baseIdx + 4].toInt()
                    val ch5 = buffer[baseIdx + 5].toInt()
                    val rawMixed = ((ch2 + ch3 + ch4 + ch5) / 4).toShort()
                    rawMono[i * 2] = (rawMixed.toInt() and 0xFF).toByte()
                    rawMono[i * 2 + 1] = (rawMixed.toInt() shr 8 and 0xFF).toByte()
                }

                // Send raw PCM (0x01) immediately for Gemini Assistant / recording
                val rawFrame = ByteArray(rawMono.size + 1)
                rawFrame[0] = FRAME_TYPE_AUDIO
                System.arraycopy(rawMono, 0, rawFrame, 1, rawMono.size)
                webSocketRef.get()?.send(rawFrame.toByteString())

                // Send post-algorithm PCM (0x05)
                val postAlgFrame = ByteArray(postAlgMono.size + 1)
                postAlgFrame[0] = FRAME_TYPE_AUDIO_POST_ALGORITHM
                System.arraycopy(postAlgMono, 0, postAlgFrame, 1, postAlgMono.size)
                webSocketRef.get()?.send(postAlgFrame.toByteString())

                val inIndex = try {
                    codec.dequeueInputBuffer(10_000)
                } catch (e: IllegalStateException) { break }
                if (inIndex >= 0) {
                    val inBuf = codec.getInputBuffer(inIndex) ?: continue
                    inBuf.clear()
                    inBuf.put(rawMono, 0, rawMono.size)
                    codec.queueInputBuffer(inIndex, 0, rawMono.size, System.nanoTime() / 1000, 0)
                }
            }
        }

        // Drain AAC encoder output and send as 0x04
        aacDrainJob = viewModelScope.launch(Dispatchers.IO) {
            val bufferInfo = MediaCodec.BufferInfo()
            while (_connectionState.value == ConnectionState.CONNECTED) {
                val outIndex = try {
                    codec.dequeueOutputBuffer(bufferInfo, 10_000)
                } catch (e: IllegalStateException) { break }
                if (outIndex >= 0) {
                    val outBuf = codec.getOutputBuffer(outIndex)
                    if (outBuf != null && bufferInfo.size > 0) {
                        val aacBytes = ByteArray(bufferInfo.size)
                        outBuf.position(bufferInfo.offset)
                        outBuf.limit(bufferInfo.offset + bufferInfo.size)
                        outBuf.get(aacBytes)

                        // Wrap each raw AAC frame in an ADTS header for server-side muxing
                        val adts = addAdtsHeader(aacBytes, MIC_SAMPLE_RATE, 1)
                        val frame = ByteArray(adts.size + 1)
                        frame[0] = FRAME_TYPE_AUDIO_AAC
                        System.arraycopy(adts, 0, frame, 1, adts.size)
                        webSocketRef.get()?.send(frame.toByteString())
                    }
                    codec.releaseOutputBuffer(outIndex, false)
                }
            }
        }
    }

    /**
     * Wrap a raw AAC frame in a 7-byte ADTS header so ffmpeg can demux it.
     */
    private fun addAdtsHeader(aacData: ByteArray, sampleRate: Int, channels: Int): ByteArray {
        val frameLen = aacData.size + 7
        val freqIdx = when (sampleRate) {
            96000 -> 0; 88200 -> 1; 64000 -> 2; 48000 -> 3
            44100 -> 4; 32000 -> 5; 24000 -> 6; 22050 -> 7
            16000 -> 8; 12000 -> 9; 11025 -> 10; 8000 -> 11
            else -> 8
        }
        val header = ByteArray(7)
        header[0] = 0xFF.toByte()
        header[1] = 0xF1.toByte()           // MPEG-4, Layer 0, no CRC
        header[2] = (((2 - 1) shl 6) or (freqIdx shl 2) or (channels shr 2)).toByte() // profile=LC
        header[3] = (((channels and 3) shl 6) or (frameLen shr 11)).toByte()
        header[4] = ((frameLen shr 3) and 0xFF).toByte()
        header[5] = (((frameLen and 7) shl 5) or 0x1F).toByte()
        header[6] = 0xFC.toByte()
        val result = ByteArray(frameLen)
        System.arraycopy(header, 0, result, 0, 7)
        System.arraycopy(aacData, 0, result, 7, aacData.size)
        return result
    }

    // ---------------------------------------------------------------------------
    // Server audio playback
    // ---------------------------------------------------------------------------

    private fun playAudio(pcmData: ByteArray) {
        if (audioTrack == null) {
            val minBuf = AudioTrack.getMinBufferSize(
                PLAYBACK_SAMPLE_RATE, PLAYBACK_CHANNEL, PLAYBACK_FORMAT
            )
            audioTrack = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(PLAYBACK_SAMPLE_RATE)
                        .setChannelMask(PLAYBACK_CHANNEL)
                        .setEncoding(PLAYBACK_FORMAT)
                        .build()
                )
                .setBufferSizeInBytes(minBuf)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            audioTrack?.play()
        }
        audioTrack?.write(pcmData, 0, pcmData.size)
    }

    // ---------------------------------------------------------------------------
    // Cleanup
    // ---------------------------------------------------------------------------

    private fun teardown() {
        webSocketRef.set(null)

        // Stop HEVC encoder / camera pipeline.
        encoderJob?.cancel()
        encoderJob = null
        try {
            captureSession?.stopRepeating()
        } catch (_: Exception) {
        }
        captureSession?.close()
        captureSession = null
        cameraDevice?.close()
        cameraDevice = null
        try {
            encoderSurface?.release()
        } catch (_: Exception) {
        }
        encoderSurface = null
        try {
            encoder?.stop()
        } catch (_: Exception) {
        }
        try {
            encoder?.release()
        } catch (_: Exception) {
        }
        encoder = null

        // Tear down camera thread/handler.
        try {
            cameraThread?.quitSafely()
        } catch (_: Exception) {
        }
        cameraThread = null
        cameraHandler = null

        micJob?.cancel()
        micJob = null
        aacDrainJob?.cancel()
        aacDrainJob = null

        try { aacEncoder?.stop() } catch (_: Exception) {}
        try { aacEncoder?.release() } catch (_: Exception) {}
        aacEncoder = null

        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null

        audioTrack?.stop()
        audioTrack?.release()
        audioTrack = null

        _connectionState.value = ConnectionState.DISCONNECTED
        _statusMessage.value = "Disconnected"
        _serverMessage.value = ""
    }

    override fun onCleared() {
        super.onCleared()
        disconnect()
        httpClient.dispatcher.executorService.shutdown()
    }
}
