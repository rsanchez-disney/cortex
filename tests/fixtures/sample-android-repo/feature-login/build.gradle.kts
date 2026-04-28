plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.sampleandroid.feature.login"
    compileSdk = 34
}

dependencies {
    implementation(project(":core"))
    api("com.squareup.retrofit2:retrofit:2.9.0")
}
