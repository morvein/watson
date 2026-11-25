<p align="center">
  <img src=".github/assets/watsonbanner.png" alt="Watson" width="620">
</p>

<h1 align="center">Watson</h1>

<p align="center">
  <strong>Phone account lookups with clear terminal results.</strong><br>
  Check supported services, follow live progress, and export results as JSON.
</p>

<p align="center">
  <code>Python 3.11+</code> &nbsp;·&nbsp;
  <code>Windows CLI</code> &nbsp;·&nbsp;
  <code>Text &amp; JSON</code> &nbsp;·&nbsp;
  <code>7 services</code>
</p>

<p align="center">
  <a href="#demo">Demo</a> &nbsp;·&nbsp;
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="#supported-services">Services</a> &nbsp;·&nbsp;
  <a href="#usage">Usage</a> &nbsp;·&nbsp;
  <a href="#options">Options</a>
</p>

---

## Demo

<p align="center">
  <img src=".github/assets/watsongif.gif" alt="Watson command, live progress, and lookup results in the terminal" width="686">
</p>

The recording shows one run. Results depend on each service's response at the time of the lookup.

## Quick start

**Requirements:** Python 3.11 or newer. Install Google Chrome to use the Amazon and Snapchat checks.

From the Watson folder, create the environment and install the dependencies:

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.\watson.cmd PHONE_NUMBER
```

Replace `PHONE_NUMBER` with your own number in international format, including `+`.
Use `--region` with an ISO country code if the number has no international prefix.

To use `watson` from any directory, add the folder containing `watson.cmd` to your user `PATH` and open a new terminal. The launcher uses the local `.venv`; activating it manually is unnecessary.

## Supported services

| Service | Select with | Runs by default |
| --- | --- | :---: |
| Instagram | `--instagram` | Yes |
| Amazon | `--amazon` | Yes |
| Microsoft | `--microsoft` | Yes |
| Facebook | `--facebook` | Yes |
| Odnoklassniki | `--odnoklassniki` or `--ok` | Yes |
| Snapchat | `--snapchat` | Yes |
| Zoho | `--zoho` | No — select explicitly |

Select one or combine several service flags. The default run checks **six services**; Zoho runs only when requested.

## Usage

### Run a lookup

```powershell
# Default services
watson PHONE_NUMBER

# Select services
watson PHONE_NUMBER --instagram --microsoft

# Include additional fields and per-service timing
watson PHONE_NUMBER --details

# Check Zoho separately
watson PHONE_NUMBER --zoho
```

The comma-separated form is also supported: `watson PHONE_NUMBER --services instagram,amazon`.
Use either `--services` or individual service flags in a command.

### Export results

```powershell
# Print compact JSON
watson PHONE_NUMBER --json

# Save JSON while keeping normal terminal output
watson PHONE_NUMBER --output results.json
```

Nothing is saved by default. With `--output`, the file is updated as checks finish.

### Inspect before running

These commands do not send the number to a service:

```powershell
# List available methods and requirements
watson methods

# Preview the selected checks
watson PHONE_NUMBER --dry-run

# Inspect number formatting and metadata
watson normalize PHONE_NUMBER
```

## Reading the output

The live progress bar shows completed checks, elapsed time, estimated remaining time, and checks per second.

| Marker | Terminal color | Meaning |
| :---: | --- | --- |
| `[+]` | Green | Registered — the service recognized the identifier at the lookup step |
| `[-]` | Purple | Not registered — the service did not find the identifier at that step |
| `[?]` | Red | Rate limit — no registration conclusion |
| `[!]` | Red | Unknown, authentication required, or manual action required |
| `[x]` | Red | Error — the check could not complete |

Available Odnoklassniki profile fields and Microsoft email hints appear below their service rows. Use `--details` for all available fields and per-service timing. Masked values stay masked; account data retains the service's original language.

<details>
<summary><strong>Example JSON output</strong></summary>

```powershell
watson +12025550123 --instagram --microsoft --json
```

```json
{
  "instagram": {"status": "registered"},
  "microsoft": {"status": "not registered"}
}
```

The number and results in this JSON example are fictional. JSON includes each service's status and any returned fields, omitting timestamps, method descriptions, and empty fields.

</details>

## Options

| Option or command | Purpose |
| --- | --- |
| `--json` | Print compact JSON instead of normal terminal output |
| `--output FILE` | Save results as JSON as checks complete |
| `--details` | Show additional fields and per-service timing |
| `--region CODE` | Set an ISO region for a number without `+` |
| `--dry-run` | Show the plan without network requests |
| `--timeout SECONDS` | Set an overall timeout; unlimited by default |
| `--amazon-interactive` | Open Chrome for manual Amazon CAPTCHA |
| `--snapchat-interactive` | Open Chrome for manual Snapchat CAPTCHA |
| `methods` | List lookup methods and requirements |
| `normalize PHONE_NUMBER` | Show number formats and metadata locally |
| `open SERVICE` | Open a service's official page |

The short form `watson PHONE_NUMBER` allows identifier/recovery requests. With the explicit `check` subcommand, add `--allow-recovery`:

```powershell
watson check PHONE_NUMBER --instagram --allow-recovery
```

Press **Ctrl+C** to cancel. Each network operation has its own timeout.

## Lookup behavior

Checks stop before password submission or SMS/OTP requests. CAPTCHA alone does not confirm registration. A result applies to the specific sign-in or recovery step and does not verify number ownership. Service responses can change.

Browser checks use temporary sessions rather than a personal Chrome profile.

---

Third-party attribution and license texts are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
