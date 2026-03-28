package com.rokid.cxrssdksamples.activities.connect

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.rokid.cxrssdksamples.theme.CXRSSDKSamplesTheme

class ConnectActivity : ComponentActivity() {
    private val viewModel: ConnectViewModel by viewModels()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        if (grants.values.all { it }) {
            viewModel.connect()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        setContent {
            CXRSSDKSamplesTheme(darkTheme = true) {
                val connectionState by viewModel.connectionState.collectAsState()
                val statusMessage by viewModel.statusMessage.collectAsState()
                val serverMessage by viewModel.serverMessage.collectAsState()

                ConnectScreen(
                    connectionState = connectionState,
                    statusMessage = statusMessage,
                    serverMessage = serverMessage,
                    onConnectClick = { handleConnectClick() },
                    onDisconnectClick = { viewModel.disconnect() }
                )
            }
        }
    }

    private fun handleConnectClick() {
        val needed = listOf(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CAMERA,
        ).filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isEmpty()) {
            viewModel.connect()
        } else {
            permissionLauncher.launch(needed.toTypedArray())
        }
    }
}

@Composable
fun ConnectScreen(
    connectionState: ConnectionState,
    statusMessage: String,
    serverMessage: String,
    onConnectClick: () -> Unit,
    onDisconnectClick: () -> Unit,
) {
    val isConnected = connectionState == ConnectionState.CONNECTED
    val isConnecting = connectionState == ConnectionState.CONNECTING

    val statusColor = when (connectionState) {
        ConnectionState.CONNECTED -> Color.Green
        ConnectionState.CONNECTING -> Color.Yellow
        ConnectionState.DISCONNECTED -> Color(0xFF888888)
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // Status indicator dot + label
        Text(
            text = when (connectionState) {
                ConnectionState.CONNECTED -> "● Connected"
                ConnectionState.CONNECTING -> "● Connecting…"
                ConnectionState.DISCONNECTED -> "● Disconnected"
            },
            color = statusColor,
            fontSize = 18.sp,
            fontWeight = FontWeight.Bold,
        )

        Text(
            text = statusMessage,
            color = Color.White,
            fontSize = 14.sp,
            textAlign = TextAlign.Center,
        )

        Spacer(modifier = Modifier.height(32.dp))

        if (serverMessage.isNotEmpty()) {
            Text(
                text = serverMessage,
                color = Color.Cyan,
                fontSize = 24.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Color.DarkGray, shape = RoundedCornerShape(8.dp))
                    .padding(16.dp)
            )

            Spacer(modifier = Modifier.height(32.dp))
        }

        if (!isConnected) {
            ActionButton(
                text = if (isConnecting) "Connecting…" else "Connect",
                enabled = !isConnecting,
                onClick = onConnectClick,
            )
        } else {
            ActionButton(
                text = "Disconnect",
                borderColor = Color.Red,
                textColor = Color.Red,
                onClick = onDisconnectClick,
            )
        }
    }
}

@Composable
fun ActionButton(
    text: String,
    enabled: Boolean = true,
    borderColor: Color = Color.Green,
    textColor: Color = Color.Green,
    onClick: () -> Unit,
) {
    OutlinedButton(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp),
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(5.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = Color.Black,
            contentColor = textColor,
            disabledContainerColor = Color.Black,
            disabledContentColor = textColor.copy(alpha = 0.3f),
        ),
        border = BorderStroke(1.dp, if (enabled) borderColor else borderColor.copy(alpha = 0.3f)),
    ) {
        Text(text = text)
    }
}
