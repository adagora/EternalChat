#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! xcodebuild -version >/dev/null 2>&1; then
  echo 'Install full Xcode and select its Developer directory before building iOS.' >&2
  exit 1
fi
if ! command -v xcodegen >/dev/null 2>&1; then
  echo 'Install XcodeGen, then run this script again.' >&2
  exit 1
fi
xcodegen generate --spec project.json
xcodebuild -project EternalChat.xcodeproj -scheme EternalChat -sdk iphonesimulator -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
