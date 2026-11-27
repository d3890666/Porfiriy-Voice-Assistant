package ru.porfiriy.watch

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

@Composable
fun PorfiriyOrbCanvas(
    phase: WatchUiPhase,
    rms: Float,
    palette: LedPalette,
    modifier: Modifier = Modifier,
    compactHeaderMode: Boolean = false,
) {
    val targetColor = when (phase) {
        WatchUiPhase.CONNECTING, WatchUiPhase.LISTENING -> palette.listenColor
        WatchUiPhase.THINKING -> palette.thinkColor
        WatchUiPhase.RESPONDING, WatchUiPhase.DONE -> palette.speakColor
        WatchUiPhase.ERROR -> Color(0xFFEF4444)
        WatchUiPhase.PERMISSION_NEEDED -> palette.idleColor
    }

    val activeColor by animateColorAsState(
        targetValue = targetColor,
        animationSpec = tween(durationMillis = 450, easing = FastOutSlowInEasing),
        label = "orbColor",
    )

    val smoothRms by animateFloatAsState(
        targetValue = if (phase == WatchUiPhase.LISTENING) rms.coerceIn(0f, 1f) else 0f,
        animationSpec = tween(durationMillis = 85),
        label = "orbRms",
    )

    val infinite = rememberInfiniteTransition(label = "orbAnim")
    val spinDeg by infinite.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 3200, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "spinDeg",
    )
    val breathe by infinite.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1900, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "breathe",
    )
    val wavePhase by infinite.animateFloat(
        initialValue = 0f,
        targetValue = (2.0 * PI).toFloat(),
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 2200, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "wavePhase",
    )

    Canvas(modifier = modifier) {
        val minDim = size.minDimension
        val center = Offset(size.width / 2f, size.height / 2f)

        if (compactHeaderMode) {
            // Sleek luminous aurora arc & orb for response screen header
            val baseR = minDim * 0.34f
            val glowR = baseR * (1.7f + 0.25f * breathe)
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(
                        activeColor.copy(alpha = 0.55f),
                        activeColor.copy(alpha = 0.16f),
                        Color.Transparent,
                    ),
                    center = center,
                    radius = glowR,
                ),
                radius = glowR,
                center = center,
            )
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(Color.White.copy(alpha = 0.92f), activeColor),
                    center = center,
                    radius = baseR,
                ),
                radius = baseR * (0.9f + 0.12f * breathe),
                center = center,
            )
            return@Canvas
        }

        // Full-screen pixel-watch AMOLED orb
        val baseRadius = minDim * (0.21f + smoothRms * 0.08f + breathe * 0.018f)
        val haloRadius = minDim * (0.45f + smoothRms * 0.12f + breathe * 0.03f)

        // Deep outer ambient halo
        drawCircle(
            brush = Brush.radialGradient(
                colors = listOf(
                    activeColor.copy(alpha = 0.34f + smoothRms * 0.24f),
                    activeColor.copy(alpha = 0.10f + smoothRms * 0.10f),
                    Color.Transparent,
                ),
                center = center,
                radius = haloRadius,
            ),
            radius = haloRadius,
            center = center,
        )

        // Harmonic organic wave rings
        val ringCount = 3
        for (idx in 0 until ringCount) {
            val layerFactor = 1f + idx * 0.22f
            val ringRadius = baseRadius * (1.18f * layerFactor + smoothRms * (0.26f + idx * 0.10f))
            val amp = minDim * (0.010f + smoothRms * (0.028f + idx * 0.008f))
            val freq = 4 + idx * 2
            val rotOffset = wavePhase * (if (idx % 2 == 0) 1f else -1.3f)

            val path = Path()
            val steps = 72
            for (s in 0..steps) {
                val theta = (s.toFloat() / steps) * (2.0 * PI).toFloat()
                val ripple = sin(theta * freq + rotOffset) * amp +
                    cos(theta * (freq - 1) - wavePhase * 1.6f) * (amp * 0.45f)
                val r = ringRadius + ripple
                val x = center.x + cos(theta) * r
                val y = center.y + sin(theta) * r
                if (s == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            path.close()

            val alpha = (0.58f - idx * 0.15f + smoothRms * 0.22f).coerceIn(0.12f, 0.90f)
            drawPath(
                path = path,
                color = activeColor.copy(alpha = alpha),
                style = Stroke(width = (2.6f - idx * 0.55f).coerceAtLeast(1.2f)),
            )
        }

        // Dual sweeping orbital arcs during THINKING / CONNECTING
        if (phase == WatchUiPhase.THINKING || phase == WatchUiPhase.CONNECTING) {
            val arcR = baseRadius * 1.48f
            val topLeft = Offset(center.x - arcR, center.y - arcR)
            val arcSize = Size(arcR * 2f, arcR * 2f)
            drawArc(
                color = activeColor.copy(alpha = 0.92f),
                startAngle = spinDeg,
                sweepAngle = 95f,
                useCenter = false,
                topLeft = topLeft,
                size = arcSize,
                style = Stroke(width = 4.2f, cap = StrokeCap.Round),
            )
            drawArc(
                color = Color.White.copy(alpha = 0.72f),
                startAngle = spinDeg + 180f,
                sweepAngle = 70f,
                useCenter = false,
                topLeft = topLeft,
                size = arcSize,
                style = Stroke(width = 3.0f, cap = StrokeCap.Round),
            )
        }

        // Luminous inner core sphere
        drawCircle(
            brush = Brush.radialGradient(
                colors = listOf(
                    Color.White.copy(alpha = 0.95f),
                    activeColor.copy(alpha = 0.95f),
                    activeColor.copy(alpha = 0.35f),
                ),
                center = Offset(center.x - baseRadius * 0.15f, center.y - baseRadius * 0.15f),
                radius = baseRadius * 1.05f,
            ),
            radius = baseRadius,
            center = center,
        )
    }
}
