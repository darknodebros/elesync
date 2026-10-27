# scripts/Release-Recovery.ps1
# Run from the EleSync repository root.

param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.23.76",

    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$ExpectedOldVersion = "1.23.75"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Tag = "v$Version"

function Write-Section {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title
    )

    Write-Host ""
    Write-Host "=============================================="
    Write-Host " $Title"
    Write-Host "=============================================="
    Write-Host ""
}

function Test-CommandAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter()]
        [string[]]$Arguments = @(),

        [Parameter()]
        [int[]]$AllowedExitCodes = @(0),

        [Parameter()]
        [switch]$CaptureOutput,

        [Parameter()]
        [switch]$Quiet
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        if ($CaptureOutput -or $Quiet) {
            $output = & $Command @Arguments 2>&1
        }
        else {
            & $Command @Arguments
            $output = @()
        }

        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($AllowedExitCodes -notcontains $exitCode) {
        $message = "Command failed with exit code $exitCode`: $Command $($Arguments -join ' ')"

        if ($output.Count -gt 0) {
            $message += "`n$($output -join [Environment]::NewLine)"
        }

        throw $message
    }

    if ($CaptureOutput) {
        return [PSCustomObject]@{
            ExitCode = $exitCode
            Output   = (($output | Out-String).Trim())
            Lines    = @($output)
        }
    }

    return [PSCustomObject]@{
        ExitCode = $exitCode
        Output   = ""
        Lines    = @()
    }
}

function Invoke-Required {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter()]
        [string[]]$Arguments = @(),

        [Parameter()]
        [switch]$CaptureOutput,

        [Parameter()]
        [switch]$Quiet
    )

    return Invoke-Native `
        -Command $Command `
        -Arguments $Arguments `
        -AllowedExitCodes @(0) `
        -CaptureOutput:$CaptureOutput `
        -Quiet:$Quiet
}

function Get-RequiredOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter()]
        [string[]]$Arguments = @()
    )

    $result = Invoke-Required `
        -Command $Command `
        -Arguments $Arguments `
        -CaptureOutput

    return $result.Output
}

function Test-GitTrackedChanges {
    $result = Invoke-Native `
        -Command "git" `
        -Arguments @("diff", "--quiet") `
        -AllowedExitCodes @(0, 1) `
        -Quiet

    return $result.ExitCode -eq 1
}

function Test-GitStagedChanges {
    $result = Invoke-Native `
        -Command "git" `
        -Arguments @("diff", "--cached", "--quiet") `
        -AllowedExitCodes @(0, 1) `
        -Quiet

    return $result.ExitCode -eq 1
}

function Test-RemoteTagExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Remote,

        [Parameter(Mandatory = $true)]
        [string]$TagName
    )

    $result = Invoke-Native `
        -Command "git" `
        -Arguments @(
            "ls-remote",
            "--exit-code",
            "--tags",
            $Remote,
            "refs/tags/$TagName"
        ) `
        -AllowedExitCodes @(0, 2) `
        -CaptureOutput

    return $result.ExitCode -eq 0 -and `
        -not [string]::IsNullOrWhiteSpace($result.Output)
}

function Test-LocalTagExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TagName
    )

    $output = Get-RequiredOutput `
        -Command "git" `
        -Arguments @("tag", "--list", $TagName)

    return -not [string]::IsNullOrWhiteSpace($output)
}

function Test-GitHubReleaseExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TagName
    )

    $result = Invoke-Required `
        -Command "gh" `
        -Arguments @(
            "release",
            "list",
            "--limit",
            "100",
            "--json",
            "tagName"
        ) `
        -CaptureOutput

    if ([string]::IsNullOrWhiteSpace($result.Output)) {
        return $false
    }

    $releases = $result.Output | ConvertFrom-Json

    return @($releases).tagName -contains $TagName
}

function Wait-ForWorkflowRun {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Workflow,

        [Parameter(Mandatory = $true)]
        [string]$CommitSha,

        [Parameter()]
        [int]$Attempts = 60,

        [Parameter()]
        [int]$DelaySeconds = 2
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $result = Invoke-Required `
            -Command "gh" `
            -Arguments @(
                "run",
                "list",
                "--workflow",
                $Workflow,
                "--commit",
                $CommitSha,
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,status,conclusion,event"
            ) `
            -CaptureOutput

        if (-not [string]::IsNullOrWhiteSpace($result.Output)) {
            $runs = @($result.Output | ConvertFrom-Json)

            $matchingRun = $runs |
                Where-Object { $_.headSha -eq $CommitSha } |
                Select-Object -First 1

            if ($null -ne $matchingRun) {
                return [string]$matchingRun.databaseId
            }
        }

        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    return $null
}

function Assert-WorkflowSuccess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    Write-Host "Watching $Description run $RunId..."

    $watch = Invoke-Native `
        -Command "gh" `
        -Arguments @(
            "run",
            "watch",
            $RunId,
            "--exit-status"
        ) `
        -AllowedExitCodes @(0, 1)

    if ($watch.ExitCode -ne 0) {
        $details = Invoke-Required `
            -Command "gh" `
            -Arguments @(
                "run",
                "view",
                $RunId,
                "--json",
                "url,status,conclusion,name,headSha"
            ) `
            -CaptureOutput

        throw "$Description failed.`n$($details.Output)"
    }

    $result = Invoke-Required `
        -Command "gh" `
        -Arguments @(
            "run",
            "view",
            $RunId,
            "--json",
            "conclusion,status,url,headSha,name"
        ) `
        -CaptureOutput

    $run = $result.Output | ConvertFrom-Json

    if ($run.conclusion -ne "success") {
        throw "$Description did not succeed. Conclusion: $($run.conclusion). URL: $($run.url)"
    }

    Write-Host "$Description succeeded."
    Write-Host "  $($run.url)"
}

function Assert-CleanWorkingTree {
    $status = Get-RequiredOutput `
        -Command "git" `
        -Arguments @("status", "--porcelain")

    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host "Working tree contains changes:"
        Invoke-Required "git" @("status", "--short")

        throw "Working tree must be clean before continuing."
    }
}

Write-Section "EleSync $Tag Release Recovery"

foreach ($command in @("git", "python", "gh")) {
    if (-not (Test-CommandAvailable $command)) {
        throw "Required command '$command' is not available in PATH."
    }
}

$insideRepo = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("rev-parse", "--is-inside-work-tree")

if ($insideRepo -ne "true") {
    throw "Run this script from inside the EleSync Git repository."
}

$repoRoot = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("rev-parse", "--show-toplevel")

$currentDirectory = (Get-Location).Path

Write-Host "Repository : $repoRoot"
Write-Host "Directory  : $currentDirectory"
Write-Host "From       : $ExpectedOldVersion"
Write-Host "To         : $Version"
Write-Host "Target tag : $Tag"

Assert-CleanWorkingTree

Write-Section "GitHub Authentication"

Invoke-Required "gh" @("auth", "status")

Write-Section "Synchronizing master"

$currentBranch = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("branch", "--show-current")

if ($currentBranch -ne "master") {
    Invoke-Required "git" @("checkout", "master")
}

Invoke-Required "git" @("fetch", "origin")
Invoke-Required "git" @("pull", "--ff-only", "origin", "master")

Assert-CleanWorkingTree

Write-Section "Checking Current Version"

$currentVersionPython = @'
import tomllib

with open("pyproject.toml", "rb") as file:
    print(tomllib.load(file)["project"]["version"])
'@

$currentVersionOutput = $currentVersionPython | python -

if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the current version from pyproject.toml."
}

$CurrentVersion = ($currentVersionOutput | Out-String).Trim()

Write-Host "Current version: $CurrentVersion"
Write-Host "Target version : $Version"

if ($CurrentVersion -ne $ExpectedOldVersion) {
    throw "Expected current version '$ExpectedOldVersion', but found '$CurrentVersion'. Update the script parameters before proceeding."
}

Write-Section "Checking Target Release Availability"

if (Test-LocalTagExists -TagName $Tag) {
    throw "Local tag '$Tag' already exists. Do not reuse release tags."
}

if (Test-RemoteTagExists -Remote "origin" -TagName $Tag) {
    throw "Remote tag '$Tag' already exists. Use the next patch version."
}

if (Test-GitHubReleaseExists -TagName $Tag) {
    throw "GitHub Release '$Tag' already exists. Use the next patch version."
}

Write-Host "$Tag is available."

Write-Section "Updating Release Metadata"

$env:ELESYNC_NEW_VERSION = $Version
$env:ELESYNC_OLD_VERSION = $ExpectedOldVersion

$versionUpdateScript = @'
from __future__ import annotations

import os
import re
from pathlib import Path

new_version = os.environ["ELESYNC_NEW_VERSION"]
old_version = os.environ["ELESYNC_OLD_VERSION"]


def replace_required(
    path: Path,
    pattern: str,
    replacement: str,
    *,
    minimum: int = 1,
) -> int:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text)

    if count < minimum:
        raise SystemExit(
            f"Expected at least {minimum} replacement(s) in {path}, found {count}."
        )

    path.write_text(updated, encoding="utf-8")
    print(f"Updated {path}: {count} replacement(s)")
    return count


pyproject_path = Path("pyproject.toml")
init_path = Path("elesync/__init__.py")
index_path = Path("index.html")
readme_path = Path("README.md")

replace_required(
    pyproject_path,
    rf'(?m)^version\s*=\s*"{re.escape(old_version)}"$',
    f'version = "{new_version}"',
)

replace_required(
    init_path,
    rf'(?m)^__version__\s*=\s*"{re.escape(old_version)}"$',
    f'__version__ = "{new_version}"',
)

if index_path.exists():
    text = index_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        rf"\bv{re.escape(old_version)}\b",
        f"v{new_version}",
        text,
    )

    if count:
        index_path.write_text(updated, encoding="utf-8")
        print(f"Updated index.html: {count} replacement(s)")
    else:
        print("index.html contained no old advertised version.")

if readme_path.exists():
    text = readme_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        rf"\b{re.escape(old_version)}\b",
        new_version,
        text,
    )

    if count:
        readme_path.write_text(updated, encoding="utf-8")
        print(f"Updated README.md: {count} replacement(s)")
    else:
        print("README.md contained no old version.")
'@

$versionUpdateScript | python -

if ($LASTEXITCODE -ne 0) {
    throw "Version metadata update failed."
}

Write-Section "Updating CHANGELOG"

$changelogScript = @'
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

version = os.environ["ELESYNC_NEW_VERSION"]

path = Path("CHANGELOG.md")
text = path.read_text(encoding="utf-8")

heading = f"## [{version}]"

if heading in text:
    print(f"{heading} already exists; leaving it unchanged.")
    raise SystemExit(0)

anchor = "## [Unreleased]"

if anchor not in text:
    raise SystemExit("Could not find '## [Unreleased]' in CHANGELOG.md.")

entry = f"""
## [{version}] - {date.today().isoformat()}

### Fixed

- Release recovery for the dashboard workspace restructuring after previous release attempts did not complete the distribution pipeline.
- Ensured dashboard and workspace changes ship from a CI-green tree with aligned package and release metadata.
"""

text = text.replace(anchor, anchor + "\n" + entry, 1)
path.write_text(text, encoding="utf-8")

print(f"Added CHANGELOG section for {version}.")
'@

$changelogScript | python -

if ($LASTEXITCODE -ne 0) {
    throw "CHANGELOG update failed."
}

Write-Section "Validating Version Consistency"

$verificationScript = @'
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

expected = os.environ["ELESYNC_NEW_VERSION"]

with Path("pyproject.toml").open("rb") as file:
    project_version = tomllib.load(file)["project"]["version"]

init_text = Path("elesync/__init__.py").read_text(encoding="utf-8")

match = re.search(
    r'__version__\s*=\s*["\']([^"\']+)["\']',
    init_text,
)

if match is None:
    raise SystemExit("Unable to locate __version__ in elesync/__init__.py.")

surfaces = {
    "pyproject.toml": project_version,
    "elesync/__init__.py": match.group(1),
}

errors = []

for name, value in surfaces.items():
    if value != expected:
        errors.append(
            f"{name}: expected {expected!r}, found {value!r}"
        )

if errors:
    raise SystemExit(
        "Version mismatch:\n" + "\n".join(errors)
    )

changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

if f"## [{expected}]" not in changelog:
    raise SystemExit(
        f"CHANGELOG.md has no section for {expected}."
    )

print(f"Required release metadata agrees on {expected}.")
'@

$verificationScript | python -

if ($LASTEXITCODE -ne 0) {
    throw "Version consistency validation failed."
}

Write-Section "Release Diff"

Invoke-Required "git" @("diff", "--check")
Invoke-Required "git" @("diff", "--stat")

Invoke-Required "git" @(
    "diff",
    "--",
    "pyproject.toml",
    "elesync/__init__.py",
    "index.html",
    "README.md",
    "CHANGELOG.md"
)

Write-Section "Installing Development Tools"

Invoke-Required "python" @(
    "-m",
    "pip",
    "install",
    "--upgrade",
    "pip"
)

Invoke-Required "python" @(
    "-m",
    "pip",
    "install",
    "-e",
    ".[dev]"
)

Invoke-Required "python" @(
    "-m",
    "pip",
    "install",
    "build",
    "twine"
)

Write-Section "Running Tests"

Invoke-Required "python" @(
    "-m",
    "unittest",
    "discover",
    "-s",
    "tests",
    "-v"
)

if (-not (Test-CommandAvailable "ruff")) {
    throw "ruff is not available after installing development dependencies."
}

Write-Host ""
Write-Host "Running Ruff lint..."

Invoke-Required "ruff" @(
    "check",
    "elesync/",
    "tests/",
    "scripts/"
)

Write-Host ""
Write-Host "Running Ruff formatting validation..."

Invoke-Required "ruff" @(
    "format",
    "--check",
    "elesync/",
    "tests/",
    "scripts/"
)

Write-Host ""
Write-Host "Running MyPy..."

Invoke-Required "python" @(
    "-m",
    "mypy",
    "elesync/"
)

Write-Host ""
Write-Host "Running packaging tests..."

$packagingTest = Invoke-Native `
    -Command "python" `
    -Arguments @(
        "-m",
        "unittest",
        "tests.test_packaging",
        "-v"
    ) `
    -AllowedExitCodes @(0, 1)

if ($packagingTest.ExitCode -ne 0) {
    throw "Packaging tests failed."
}

Write-Section "Building Distribution"

if (Test-Path "dist") {
    Remove-Item "dist" -Recurse -Force
}

if (Test-Path "build") {
    Remove-Item "build" -Recurse -Force
}

Invoke-Required "python" @("-m", "build")

$distributionFiles = @(
    Get-ChildItem "dist" -File -ErrorAction Stop
)

if ($distributionFiles.Count -eq 0) {
    throw "python -m build produced no distribution files."
}

$twineArguments = @(
    "-m",
    "twine",
    "check"
)

foreach ($file in $distributionFiles) {
    $twineArguments += $file.FullName
}

Invoke-Required "python" $twineArguments

Write-Section "Pre-Commit Validation"

Invoke-Required "git" @("diff", "--check")

Write-Host "Git status:"
Invoke-Required "git" @("status", "--short")

$releaseFiles = @(
    "pyproject.toml",
    "elesync/__init__.py",
    "CHANGELOG.md"
)

foreach ($optionalFile in @("index.html", "README.md")) {
    if (Test-Path $optionalFile) {
        $releaseFiles += $optionalFile
    }
}

Invoke-Required "git" (@("add") + $releaseFiles)

if (-not (Test-GitStagedChanges)) {
    throw "No staged release changes exist."
}

Write-Section "Committing Release Preparation"

Invoke-Required "git" @(
    "commit",
    "-m",
    "release: prepare $Tag"
)

$releaseSha = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("rev-parse", "HEAD")

Write-Host "Release preparation SHA:"
Write-Host "  $releaseSha"

Write-Host ""
Write-Host "Pushing master..."

Invoke-Required "git" @(
    "push",
    "origin",
    "master"
)

Write-Section "Waiting for CI"

$ciRunId = Wait-ForWorkflowRun `
    -Workflow "ci.yml" `
    -CommitSha $releaseSha

if ([string]::IsNullOrWhiteSpace($ciRunId)) {
    throw "Could not locate ci.yml for commit $releaseSha. No release tag was created."
}

Assert-WorkflowSuccess `
    -RunId $ciRunId `
    -Description "CI"

Write-Section "Final Pre-Tag Checks"

Invoke-Required "git" @(
    "fetch",
    "origin",
    "master"
)

$localSha = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("rev-parse", "HEAD")

$remoteSha = Get-RequiredOutput `
    -Command "git" `
    -Arguments @("rev-parse", "origin/master")

if ($localSha -ne $remoteSha) {
    throw @"
Local HEAD and origin/master differ.

Local : $localSha
Remote: $remoteSha

No release tag was created.
"@
}

Assert-CleanWorkingTree

if (Test-RemoteTagExists -Remote "origin" -TagName $Tag) {
    throw "Remote tag '$Tag' appeared unexpectedly. No local tag will be created."
}

if (Test-GitHubReleaseExists -TagName $Tag) {
    throw "GitHub Release '$Tag' appeared unexpectedly. No tag will be created."
}

Write-Section "Creating Release Tag"

Invoke-Required "git" @(
    "tag",
    "-a",
    $Tag,
    $releaseSha,
    "-m",
    "EleSync $Version"
)

try {
    Invoke-Required "git" @(
        "push",
        "origin",
        $Tag
    )
}
catch {
    throw @"
Failed to push $Tag.

The local tag may now exist even though the remote push failed.

Check with:
  git tag --list "$Tag"
  git ls-remote --tags origin "refs/tags/$Tag"

Do not recreate or move the tag until you confirm its state.

Original error:
$($_.Exception.Message)
"@
}

Write-Host "$Tag points to:"
Invoke-Required "git" @(
    "show",
    "--no-patch",
    "--oneline",
    $Tag
)

Write-Section "Waiting for Release Workflow"

$releaseRunId = Wait-ForWorkflowRun `
    -Workflow "release.yml" `
    -CommitSha $releaseSha

if ([string]::IsNullOrWhiteSpace($releaseRunId)) {
    throw @"
$Tag was pushed, but release.yml could not be located.

IMPORTANT:
Do not delete, recreate, or move $Tag.

Inspect GitHub Actions. After fixing the underlying issue,
use the next patch version for another release attempt.
"@
}

try {
    Assert-WorkflowSuccess `
        -RunId $releaseRunId `
        -Description "Release workflow"
}
catch {
    throw @"
Release workflow failed after $Tag was pushed.

IMPORTANT:
Do not delete, recreate, or reuse $Tag.

Fix the release failure and use the next patch version.

$($_.Exception.Message)
"@
}

Write-Section "Verifying GitHub Release"

if (-not (Test-GitHubReleaseExists -TagName $Tag)) {
    throw @"
release.yml succeeded, but GitHub Release '$Tag' was not found.

Do not reuse the tag.
Inspect the release workflow before proceeding.
"@
}

Invoke-Required "gh" @(
    "release",
    "view",
    $Tag
)

Write-Section "Release Recovery Complete"

Write-Host "Released:"
Write-Host "  $Tag"
Write-Host ""
Write-Host "Release commit:"
Write-Host "  $releaseSha"
Write-Host ""
Write-Host "Verify the private release assets with:"
Write-Host ""
Write-Host "  gh release view $Tag"
Write-Host ""
Write-Host "Expected version:"
Write-Host "  $Version"
