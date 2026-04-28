plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.sampleandroid"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.example.sampleandroid"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    api("com.squareup.retrofit2:retrofit:2.9.0")
    kapt("com.google.dagger:hilt-compiler:2.50")
}
