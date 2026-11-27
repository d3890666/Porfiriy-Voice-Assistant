package ru.porfiriy.watch

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material.icons.rounded.Send
import androidx.compose.material.icons.rounded.Settings
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.CircularProgressIndicator
import androidx.wear.compose.material.Icon
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import kotlinx.coroutines.launch

@Composable
fun PorfiriyMainScreen(
    uiState: WatchUiState,
    palette: LedPalette,
    onTapOrb: () -> Unit,
    onContinueDialogue: () -> Unit,
    onSendToTelegram: () -> Unit,
    onRetry: () -> Unit,
    onRequestMicPermission: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    when (uiState.phase) {
        WatchUiPhase.PERMISSION_NEEDED -> {
            PermissionScreen(
                onRequestMicPermission = onRequestMicPermission,
                onOpenSettings = onOpenSettings,
            )
        }
        WatchUiPhase.RESPONDING, WatchUiPhase.DONE -> {
            ResponseScreen(
                uiState = uiState,
                palette = palette,
                onContinueDialogue = onContinueDialogue,
                onSendToTelegram = onSendToTelegram,
                onOpenSettings = onOpenSettings,
            )
        }
        WatchUiPhase.ERROR -> {
            ErrorScreen(
                uiState = uiState,
                palette = palette,
                onRetry = onRetry,
                onOpenSettings = onOpenSettings,
            )
        }
        WatchUiPhase.CONNECTING, WatchUiPhase.LISTENING, WatchUiPhase.THINKING -> {
            ListeningOrbScreen(
                uiState = uiState,
                palette = palette,
                onTapOrb = onTapOrb,
                onOpenSettings = onOpenSettings,
            )
        }
    }
}

@Composable
private fun ListeningOrbScreen(
    uiState: WatchUiState,
    palette: LedPalette,
    onTapOrb: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val noRipple = remember { MutableInteractionSource() }
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
            .clickable(interactionSource = noRipple, indication = null) {
                if (uiState.phase == WatchUiPhase.LISTENING || uiState.phase == WatchUiPhase.CONNECTING) {
                    onTapOrb()
                }
            },
    ) {
        PorfiriyOrbCanvas(
            phase = uiState.phase,
            rms = uiState.rms,
            palette = palette,
            modifier = Modifier.fillMaxSize(),
        )

        // Top subtle badge (user name + route)
        Column(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .padding(top = 18.dp, start = 28.dp, end = 28.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            val headerTitle = if (uiState.boundUserName.isNotBlank()) {
                "Порфирий • ${uiState.boundUserName}"
            } else {
                "Порфирий"
            }
            Text(
                text = headerTitle,
                color = Color.White.copy(alpha = 0.88f),
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (uiState.routeBadge.isNotBlank()) {
                Text(
                    text = uiState.routeBadge,
                    color = Color(0xFF94A3B8),
                    fontSize = 10.sp,
                    maxLines = 1,
                )
            }
        }

        // Bottom live status + settings trigger
        Column(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(bottom = 14.dp, start = 24.dp, end = 24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = uiState.statusText,
                color = Color.White,
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
                textAlign = TextAlign.Center,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (uiState.phase == WatchUiPhase.LISTENING) {
                Text(
                    text = "Тап по экрану — отправить",
                    color = Color(0xFF94A3B8),
                    fontSize = 10.sp,
                    textAlign = TextAlign.Center,
                )
            }
            Spacer(modifier = Modifier.height(4.dp))
            Box(
                modifier = Modifier
                    .size(28.dp)
                    .clip(CircleShape)
                    .background(Color.White.copy(alpha = 0.08f))
                    .clickable { onOpenSettings() },
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = Icons.Rounded.Settings,
                    contentDescription = "Настройки",
                    tint = Color(0xFFCBD5E1),
                    modifier = Modifier.size(15.dp),
                )
            }
        }
    }
}

@Composable
private fun ResponseScreen(
    uiState: WatchUiState,
    palette: LedPalette,
    onContinueDialogue: () -> Unit,
    onSendToTelegram: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val scrollState = rememberScrollState()
    val focusRequester = remember { FocusRequester() }
    val coroutineScope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        focusRequester.requestFocus()
    }

    // Gentle auto-scroll down while text streams in
    LaunchedEffect(uiState.replyText.length) {
        if (uiState.phase == WatchUiPhase.RESPONDING && scrollState.maxValue > 0) {
            scrollState.animateScrollTo(scrollState.maxValue)
        }
    }

    Scaffold(
        positionIndicator = { PositionIndicator(scrollState = scrollState) },
        timeText = { TimeText() },
        modifier = Modifier.background(Color.Black),
    ) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(Color.Black)
                .onRotaryScrollEvent { event ->
                    coroutineScope.launch {
                        scrollState.scrollTo(
                            (scrollState.value + event.verticalScrollPixels).toInt()
                                .coerceIn(0, scrollState.maxValue)
                        )
                    }
                    true
                }
                .focusRequester(focusRequester)
                .focusable(),
        ) {
            // Scrollable text column
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(scrollState)
                    .padding(start = 18.dp, end = 18.dp, top = 26.dp, bottom = 68.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.Center,
                ) {
                    PorfiriyOrbCanvas(
                        phase = uiState.phase,
                        rms = 0f,
                        palette = palette,
                        compactHeaderMode = true,
                        modifier = Modifier.size(22.dp),
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = if (uiState.boundUserName.isNotBlank()) uiState.boundUserName else "Порфирий",
                        color = palette.speakColor.copy(alpha = 0.92f),
                        fontSize = 11.sp,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                    )
                }

                if (uiState.inputTranscript.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "«${uiState.inputTranscript}»",
                        color = Color(0xFF94A3B8),
                        fontSize = 11.sp,
                        lineHeight = 14.sp,
                        textAlign = TextAlign.Center,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }

                Spacer(modifier = Modifier.height(8.dp))

                Text(
                    text = uiState.replyText.ifBlank { "…" },
                    color = Color(0xFFF8FAFC),
                    fontSize = 15.sp,
                    lineHeight = 20.sp,
                    textAlign = TextAlign.Start,
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            // Bottom gradient dock so action buttons stay crisp over scrolled text
            Box(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .background(
                        Brush.verticalGradient(
                            colors = listOf(
                                Color.Transparent,
                                Color.Black.copy(alpha = 0.90f),
                                Color.Black,
                            )
                        )
                    )
                    .padding(bottom = 10.dp, top = 14.dp, start = 14.dp, end = 14.dp),
                contentAlignment = Alignment.Center,
            ) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // Always-present "Send to TG" pill button
                    val tgColor = when (uiState.tgStatus) {
                        TgSendStatus.SENT -> Color(0xFF10B981)
                        TgSendStatus.FAILED -> Color(0xFFEF4444)
                        else -> Color(0xFF0284C7)
                    }
                    Button(
                        onClick = onSendToTelegram,
                        enabled = uiState.replyText.isNotBlank() && uiState.tgStatus != TgSendStatus.SENDING,
                        colors = ButtonDefaults.buttonColors(
                            backgroundColor = tgColor,
                            contentColor = Color.White,
                        ),
                        modifier = Modifier.height(36.dp),
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(4.dp),
                        ) {
                            when (uiState.tgStatus) {
                                TgSendStatus.SENDING -> {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(14.dp),
                                        strokeWidth = 2.dp,
                                        indicatorColor = Color.White,
                                    )
                                }
                                TgSendStatus.SENT -> {
                                    Icon(
                                        imageVector = Icons.Rounded.Check,
                                        contentDescription = "Отправлено",
                                        modifier = Modifier.size(15.dp),
                                    )
                                }
                                else -> {
                                    Icon(
                                        imageVector = Icons.Rounded.Send,
                                        contentDescription = "В Telegram",
                                        modifier = Modifier.size(14.dp),
                                    )
                                }
                            }
                            Text(
                                text = when (uiState.tgStatus) {
                                    TgSendStatus.SENT -> "В ТГ ✓"
                                    TgSendStatus.FAILED -> "Ошибка"
                                    TgSendStatus.SENDING -> "…"
                                    TgSendStatus.IDLE -> "В ТГ"
                                },
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                            )
                        }
                    }

                    // Continue dialogue button
                    Button(
                        onClick = onContinueDialogue,
                        colors = ButtonDefaults.buttonColors(
                            backgroundColor = Color(0xFF1E293B),
                            contentColor = Color.White,
                        ),
                        modifier = Modifier.size(36.dp),
                    ) {
                        Icon(
                            imageVector = Icons.Rounded.Mic,
                            contentDescription = "Спросить ещё",
                            modifier = Modifier.size(18.dp),
                        )
                    }

                    // Discreet settings button
                    Button(
                        onClick = onOpenSettings,
                        colors = ButtonDefaults.buttonColors(
                            backgroundColor = Color(0xFF111827),
                            contentColor = Color(0xFF94A3B8),
                        ),
                        modifier = Modifier.size(32.dp),
                    ) {
                        Icon(
                            imageVector = Icons.Rounded.Settings,
                            contentDescription = "Настройки",
                            modifier = Modifier.size(15.dp),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ErrorScreen(
    uiState: WatchUiState,
    palette: LedPalette,
    onRetry: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
        contentAlignment = Alignment.Center,
    ) {
        PorfiriyOrbCanvas(
            phase = WatchUiPhase.ERROR,
            rms = 0f,
            palette = palette,
            modifier = Modifier.fillMaxSize(),
        )
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(22.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                text = uiState.errorMessage.ifBlank { "Нет связи" },
                color = Color(0xFFFCA5A5),
                fontSize = 13.sp,
                lineHeight = 17.sp,
                textAlign = TextAlign.Center,
                maxLines = 4,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(modifier = Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = onRetry,
                    colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF1E293B)),
                    modifier = Modifier.size(42.dp),
                ) {
                    Icon(Icons.Rounded.Refresh, contentDescription = "Повторить", tint = Color.White)
                }
                Button(
                    onClick = onOpenSettings,
                    colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF0F172A)),
                    modifier = Modifier.size(42.dp),
                ) {
                    Icon(Icons.Rounded.Settings, contentDescription = "Настройки", tint = Color(0xFFCBD5E1))
                }
            }
        }
    }
}

@Composable
private fun PermissionScreen(
    onRequestMicPermission: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = "Разрешите микрофон, чтобы говорить с Порфирием",
            color = Color.White,
            fontSize = 13.sp,
            textAlign = TextAlign.Center,
        )
        Spacer(modifier = Modifier.height(12.dp))
        Button(
            onClick = onRequestMicPermission,
            colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF0284C7)),
            modifier = Modifier.height(38.dp),
        ) {
            Text(
                text = "Разрешить",
                fontSize = 12.sp,
                modifier = Modifier.padding(horizontal = 12.dp),
            )
        }
        Spacer(modifier = Modifier.height(8.dp))
        Box(
            modifier = Modifier
                .clip(RoundedCornerShape(12.dp))
                .clickable { onOpenSettings() }
                .padding(horizontal = 10.dp, vertical = 4.dp),
        ) {
            Text("Настройки", color = Color(0xFF94A3B8), fontSize = 11.sp)
        }
    }
}
