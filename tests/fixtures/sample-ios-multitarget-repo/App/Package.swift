// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MultiApp",
    platforms: [
        .iOS(.v16)
    ],
    dependencies: [
        .package(url: "https://github.com/Alamofire/Alamofire.git", from: "5.8.0"),
    ],
    targets: [
        .executableTarget(name: "FanApp", dependencies: ["Alamofire"]),
        .executableTarget(name: "StaffApp", dependencies: ["Alamofire"]),
        .testTarget(name: "FanAppTests", dependencies: ["FanApp"]),
    ]
)
