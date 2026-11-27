plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val releaseKeystoreFile = file("${System.getProperty("user.home")}/.android/porfiriy-watch.jks")

android {
    namespace = "ru.porfiriy.watch"
    compileSdk = 34

    defaultConfig {
        applicationId = "ru.porfiriy.watch"
        minSdk = 30
        targetSdk = 34
        versionCode = 3
        versionName = "1.0.2"
    }

    signingConfigs {
        create("release") {
            if (releaseKeystoreFile.exists()) {
                storeFile = releaseKeystoreFile
                storePassword = System.getenv("PORFIRIY_KS_PASS") ?: "porfiriy2026"
                keyAlias = "porfiriy"
                keyPassword = System.getenv("PORFIRIY_KEY_PASS") ?: "porfiriy2026"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = if (releaseKeystoreFile.exists()) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.3")

    // Compose & Wear OS
    implementation("androidx.compose.ui:ui:1.6.8")
    implementation("androidx.compose.ui:ui-tooling-preview:1.6.8")
    implementation("androidx.compose.material:material-icons-extended:1.6.8")
    implementation("androidx.wear.compose:compose-material:1.3.1")
    implementation("androidx.wear.compose:compose-foundation:1.3.1")

    // Wear RemoteInput (keyboard on paired phone / watch) & Complications
    implementation("androidx.wear:wear-input:1.1.0")
    implementation("androidx.wear.watchface:watchface-complications-data-source-ktx:1.2.1")

    // Networking & Async
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
