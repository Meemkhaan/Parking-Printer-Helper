plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.reachsolutions.parkingprinter"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.reachsolutions.parkingprinter"
        minSdk = 26
        targetSdk = 34
        val runNumber = System.getenv("GITHUB_RUN_NUMBER")
        versionCode = if (runNumber != null) runNumber.toInt() else 1
        versionName = "1.0.${versionCode}"
    }

    buildTypes {
        debug {
            buildConfigField("String", "PRINT_HELPER_SECRET", "\"${System.getenv("PRINT_HELPER_SECRET") ?: "dev-secret"}\"")
            buildConfigField("String", "PRINTER_IP", "\"${System.getenv("PRINTER_IP") ?: "192.168.18.100"}\"")
            buildConfigField("int", "PRINTER_PORT", "${System.getenv("PRINTER_PORT") ?: "9100"}")
            buildConfigField("int", "PRINTER_TIMEOUT", "5000")
        }
        release {
            isMinifyEnabled = false
            buildConfigField("String", "PRINT_HELPER_SECRET", "\"${System.getenv("PRINT_HELPER_SECRET") ?: "release-secret"}\"")
            buildConfigField("String", "PRINTER_IP", "\"${System.getenv("PRINTER_IP") ?: "192.168.18.100"}\"")
            buildConfigField("int", "PRINTER_PORT", "${System.getenv("PRINTER_PORT") ?: "9100"}")
            buildConfigField("int", "PRINTER_TIMEOUT", "5000")
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
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.code.gson:gson:2.10.1")
}