package ru.porfiriy.watch

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.wear.compose.material.MaterialTheme

class MainActivity : ComponentActivity() {
    private lateinit var controller: PorfiriyController
    private var showSettingsState = mutableStateOf(false)

    private val micPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        controller.onPermissionUpdated(granted, autoStart = true)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        controller = PorfiriyController(this)

        val hasMic = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED

        if (hasMic) {
            controller.onPermissionUpdated(true, autoStart = true)
        } else {
            controller.onPermissionUpdated(false, autoStart = false)
            micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }

        setContent {
            MaterialTheme {
                val uiState by controller.uiState.collectAsState()
                val settings by controller.settingsStore.settings.collectAsState()
                var showSettings by remember { showSettingsState }

                if (showSettings) {
                    SettingsScreen(
                        settings = settings,
                        probeStatus = uiState.probeStatus,
                        isProbing = uiState.isProbing,
                        onUpdatePrimaryUrl = { controller.settingsStore.updatePrimaryUrl(it) },
                        onUpdateBackupUrl = { controller.settingsStore.updateBackupUrl(it) },
                        onUpdateApiKey = { controller.settingsStore.updateApiKey(it) },
                        onUpdateDeviceName = { controller.settingsStore.updateDeviceName(it) },
                        onProbeConnection = { controller.runConnectionProbe() },
                        onBack = {
                            showSettings = false
                            controller.startFreshSession()
                        },
                    )
                } else {
                    PorfiriyMainScreen(
                        uiState = uiState,
                        palette = settings.palette,
                        onTapOrb = { controller.finishSpeaking() },
                        onContinueDialogue = { controller.continueDialogue() },
                        onSendToTelegram = { controller.sendCurrentToTelegram() },
                        onRetry = { controller.startFreshSession() },
                        onRequestMicPermission = {
                            micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                        },
                        onOpenSettings = {
                            controller.closeSession()
                            showSettings = true
                        },
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        showSettingsState.value = false
        val hasMic = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        controller.onPermissionUpdated(hasMic, autoStart = true)
    }

    override fun onStop() {
        super.onStop()
        controller.pauseActiveMic()
    }

    override fun onDestroy() {
        controller.destroy()
        super.onDestroy()
    }
}
