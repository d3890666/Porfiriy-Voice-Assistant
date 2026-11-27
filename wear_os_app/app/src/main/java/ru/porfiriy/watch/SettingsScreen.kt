package ru.porfiriy.watch

import android.app.RemoteInput
import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.Key
import androidx.compose.material.icons.rounded.NetworkCheck
import androidx.compose.material.icons.rounded.Shield
import androidx.compose.material.icons.rounded.SystemUpdate
import androidx.compose.material.icons.rounded.Watch
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.Icon
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import androidx.wear.input.RemoteInputIntentHelper
import kotlinx.coroutines.launch

private enum class EditField {
    PRIMARY_URL,
    BACKUP_URL,
    API_KEY,
    DEVICE_NAME,
}

@Composable
fun SettingsScreen(
    settings: WatchSettings,
    probeStatus: String,
    isProbing: Boolean,
    hasUpdate: Boolean,
    targetVersion: String,
    onUpdatePrimaryUrl: (String) -> Unit,
    onUpdateBackupUrl: (String) -> Unit,
    onUpdateApiKey: (String) -> Unit,
    onUpdateDeviceName: (String) -> Unit,
    onProbeConnection: () -> Unit,
    onStartAppUpdate: () -> Unit,
    onBack: () -> Unit,
) {
    val listState = rememberScalingLazyListState()
    val focusRequester = remember { FocusRequester() }
    val scope = rememberCoroutineScope()
    var activeField by remember { mutableStateOf<EditField?>(null) }

    val remoteInputLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val data = result.data ?: return@rememberLauncherForActivityResult
        val bundle = RemoteInput.getResultsFromIntent(data) ?: return@rememberLauncherForActivityResult
        val value = bundle.getCharSequence("input_value")?.toString() ?: return@rememberLauncherForActivityResult
        when (activeField) {
            EditField.PRIMARY_URL -> onUpdatePrimaryUrl(value)
            EditField.BACKUP_URL -> onUpdateBackupUrl(value)
            EditField.API_KEY -> onUpdateApiKey(value)
            EditField.DEVICE_NAME -> onUpdateDeviceName(value)
            null -> {}
        }
        activeField = null
    }

    fun launchTextPrompt(field: EditField, label: String, currentValue: String) {
        activeField = field
        val remoteInput = RemoteInput.Builder("input_value")
            .setLabel(label)
            .build()
        val intent: Intent = RemoteInputIntentHelper.createActionRemoteInputIntent()
        RemoteInputIntentHelper.putRemoteInputsExtra(intent, listOf(remoteInput))
        RemoteInputIntentHelper.putTitleExtra(intent, currentValue.ifBlank { label })
        remoteInputLauncher.launch(intent)
    }

    LaunchedEffect(Unit) {
        focusRequester.requestFocus()
    }

    Scaffold(
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) },
        timeText = { TimeText() },
        modifier = Modifier.background(Color.Black),
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .background(Color.Black)
                .onRotaryScrollEvent { event ->
                    scope.launch {
                        listState.scrollBy(event.verticalScrollPixels)
                    }
                    true
                }
                .focusRequester(focusRequester)
                .focusable(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            item {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.padding(top = 6.dp, bottom = 4.dp),
                ) {
                    Text(
                        text = "Настройки",
                        color = Color.White,
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Bold,
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        LedDot(settings.palette.idleColor)
                        LedDot(settings.palette.listenColor)
                        LedDot(settings.palette.thinkColor)
                        LedDot(settings.palette.speakColor)
                    }
                }
            }

            item {
                Chip(
                    onClick = onStartAppUpdate,
                    label = {
                        Text(
                            if (hasUpdate && targetVersion.isNotBlank()) "Обновить до v$targetVersion" else "Обновить APK",
                            maxLines = 1,
                        )
                    },
                    secondaryLabel = {
                        Text(
                            if (hasUpdate) "Доступна новая версия" else "Скачать с Порфирия",
                            maxLines = 1,
                        )
                    },
                    icon = { Icon(Icons.Rounded.SystemUpdate, contentDescription = null) },
                    colors = ChipDefaults.primaryChipColors(
                        backgroundColor = if (hasUpdate) Color(0xFF059669) else Color(0xFF1E3A8A),
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = {
                        launchTextPrompt(EditField.PRIMARY_URL, "Основной ws:// адрес", settings.primaryUrl)
                    },
                    label = { Text("Основной адрес", maxLines = 1) },
                    secondaryLabel = {
                        Text(settings.primaryUrl, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    },
                    icon = { Icon(Icons.Rounded.Dns, contentDescription = null) },
                    colors = ChipDefaults.secondaryChipColors(backgroundColor = Color(0xFF111827)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = {
                        launchTextPrompt(EditField.BACKUP_URL, "Резервный ws:// или wss://", settings.backupUrl)
                    },
                    label = { Text("Резервный адрес", maxLines = 1) },
                    secondaryLabel = {
                        Text(
                            settings.backupUrl.ifBlank { "Не задан" },
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    },
                    icon = { Icon(Icons.Rounded.Shield, contentDescription = null) },
                    colors = ChipDefaults.secondaryChipColors(backgroundColor = Color(0xFF111827)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = {
                        launchTextPrompt(EditField.API_KEY, "Токен доступа (API Key)", settings.apiKey)
                    },
                    label = { Text("Токен (API Key)", maxLines = 1) },
                    secondaryLabel = {
                        val masked = if (settings.apiKey.isBlank()) {
                            "Пусто (без проверки)"
                        } else {
                            "•".repeat(settings.apiKey.length.coerceIn(4, 10))
                        }
                        Text(masked, maxLines = 1)
                    },
                    icon = { Icon(Icons.Rounded.Key, contentDescription = null) },
                    colors = ChipDefaults.secondaryChipColors(backgroundColor = Color(0xFF111827)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = {
                        launchTextPrompt(EditField.DEVICE_NAME, "Имя часов", settings.deviceName)
                    },
                    label = { Text("Имя устройства", maxLines = 1) },
                    secondaryLabel = {
                        Text(settings.deviceName, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    },
                    icon = { Icon(Icons.Rounded.Watch, contentDescription = null) },
                    colors = ChipDefaults.secondaryChipColors(backgroundColor = Color(0xFF111827)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = onProbeConnection,
                    enabled = !isProbing,
                    label = { Text(if (isProbing) "Проверяю…" else "Проверить связь", maxLines = 1) },
                    secondaryLabel = if (probeStatus.isNotBlank()) {
                        { Text(probeStatus, maxLines = 2, overflow = TextOverflow.Ellipsis) }
                    } else null,
                    icon = { Icon(Icons.Rounded.NetworkCheck, contentDescription = null) },
                    colors = ChipDefaults.primaryChipColors(backgroundColor = Color(0xFF0369A1)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Chip(
                    onClick = onBack,
                    label = { Text("Назад к Порфирию", maxLines = 1) },
                    icon = { Icon(Icons.AutoMirrored.Rounded.ArrowBack, contentDescription = null) },
                    colors = ChipDefaults.secondaryChipColors(backgroundColor = Color(0xFF1E293B)),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Text(
                    text = "v${BuildConfig.VERSION_NAME} • ${settings.deviceMac}",
                    color = Color(0xFF64748B),
                    fontSize = 10.sp,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(vertical = 6.dp),
                )
            }
        }
    }
}

@Composable
private fun LedDot(color: Color) {
    Box(
        modifier = Modifier
            .size(12.dp)
            .clip(CircleShape)
            .background(color)
            .border(1.dp, Color.White.copy(alpha = 0.25f), CircleShape),
    )
}
