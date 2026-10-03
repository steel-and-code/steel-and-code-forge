# Forge v1 — Security Classification Lists

Status: specification, implemented in `forge/tools/`. Superseded rules are
marked where they were changed, so the current model and the history of it can
both be read from this file.

Scope: the Forge-wide safety baseline. Byte is the reference configuration; the
model applies to every agent. Reference material for the enforcement pipeline
described in [byte-tools.md](byte-tools.md). Both lists here are inputs to that
pipeline: secret-source patterns decide what a path is, command patterns decide
what an operation is.

## What this is and is not

These lists are a **conservative classifier**. They are not a sandbox.

A classifier inspects names, paths, and arguments and decides which of three
buckets an operation falls into. It does not contain anything. It does not
isolate a process. It does not stop a determined or creative operation from
reaching outside the workspace.

The consequence is worth stating plainly: **the permission model is only as
sound as the classifier, and the classifier is incomplete by construction.**
Commands can be obfuscated, wrappers can hide behaviour, interpreters can be
given arbitrary code, and any pattern list will miss forms it did not anticipate.

What the classifier does provide is a strong default. Operations it recognises
are gated correctly. Operations it does not recognise become *ambiguous*, and
ambiguous is approval-gated. So the failure mode of a miss is a prompt, not
silent execution — provided the matching rules below are honoured.

That property is the point. The list is a convenience for the common cases
and a backstop for the rest. It is not a security boundary, and it should never
be described as one.

> **This paragraph described the model as it was designed, and no longer
> describes how it ships.** An earlier version classified an unrecognised
> argv invocation as automatic and treated that as an acceptable convenience.
> It was the single largest soft spot in the design — acknowledged as item 9 of
> the uncertainties below — because a classifier miss landed as silent
> execution rather than as a prompt. **Unknown invocations are now
> approval-required**, which restores the property this section claimed. See
> [Unknown commands](#unknown-commands).

## Matching model

Both lists match against a **structured invocation**, not a shell string:

```
{ program: str, args: list[str], cwd: str, shell: bool }
```

Rules:

1. **Secret-source patterns match on resolved paths.** Resolve to a real path
   first — symlinks followed, `..` collapsed — then match. Matching the literal
   string is not sufficient; `/repo/link-to-home/.env` and `/repo/../.env` are
   the same file.
2. **Command patterns match program basename plus all arguments.** Every
   argument is examined, not just the first. Dangerous flags and paths appear in
   any position.
3. **`shell: true` is itself a classification input.** A shell string is not
   parsed. It is marked ambiguous and approval-gated, always, regardless of
   content. This is not a gap to be closed later; it is the chosen boundary.
4. **Every match is evaluated. The strictest match wins.** One pattern saying
   "denied" outranks several saying "approval-required".
5. **A secret-source match is applied at its own tier.** A structural or
   content-proven match is **denied** with no approval path. A name-guess match
   is **approval-required**, resolved per the principle above.
6. **Automatic is an explicit rule, never an absence.** An operation is
   automatic only because a rule said so. See [List 3 — Automatic operations](#list-3--automatic-operations).

## The principle for classifying secret sources

Two things can make a file secret-bearing: **where it is** and **what it is
called**. They carry very different amounts of evidence, and v1 treats them
differently.

> **Location or structure proves sensitivity → deny.**
> **Filename merely suggests sensitivity → approval-required.**

### Why the distinction

A denial that fires on a file holding nothing is not a small annoyance. It
teaches the operator that the rule is arbitrary, and the natural response to an
arbitrary rule is to disable it. A rule that is wrong often is a rule that
survives.

The reverse asymmetry matters too. Approving a read of `credentials.json` is a
real cost — a prompt — but it is a cost paid only when someone is present, and
it is paid in exchange for the operator making an informed choice.

`~/.ssh/id_ed25519` proves its own sensitivity: the location and format together
mean one thing. `secrets.json` in a project root could hold a live API key or
could be sample data committed for documentation. Structure answers the first;
a filename cannot answer the second.

### What this changes operationally

- **Structural patterns are denied.** No prompt, no path to approve. If it is
  structurally a secret, an approval flow would be theatre.
- **Name-guess patterns are approval-required.** The operator sees the file
  name and location and decides.
- **Content-proven material is denied.** A `PRIVATE KEY` block proves
  sensitivity regardless of what the file is called.

## List 1 — Secret-source patterns

Denied. Not approval-gated. An operator approving a read of a private key is
still a secret exposed to the agent.

False positives are noted because they cost something real: a legitimate file
that gets denied. Where a pattern is likely to misfire, that is stated, and in
several cases a narrower alternative is given.

### `.env` and variants

| Pattern | Why | False positives |
| --- | --- | --- |
| `.env` | The conventional local secret file. | None in practice. |
| `.env.*` | `.env.local`, `.env.staging`, `.env.production` are all secret-bearing. `.env.example` and `.env.template` are the exceptions — they are checked into repos precisely because they hold no values. | `.env.example`, `.env.template`, `.env.sample`, `.env.dist` are false positives. **Suggested handling:** match `.env*` and exclude an explicit allowlist of example/template/sample/dist suffixes, rather than trying to match the positives exactly. |
| `*.env` | `production.env`, `app.env` — same content, different name. | `.env` variants only; a file named `something.env` that holds no secrets is unusual enough to not worry about. |
| `.envrc` | direnv environment loader; frequently holds exported secrets. | Rarely anything else. |

### SSH

| Pattern | Why | False positives |
| --- | --- | --- |
| `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` | Default private key filenames. | Low. A public key is `id_rsa.pub`. |
| `id_*` without `.pub` | Catches non-default key names (`id_work`, `id_prod`). | Moderate — could match a directory or unrelated file starting `id_`. Recommend requiring the file to look key-shaped, or accepting the over-match on a deny list. |
| `*.pem` | Conventional private key container. | **Real concern: `.pem` often holds only a public certificate.** See the PEM/certificate subsection below. |
| `*.ppk` | PuTTY private key. | None. |
| `~/.ssh/**` | Everything under the SSH directory is sensitive, including `config`, `known_hosts`, and `authorized_keys`. | **`.pub` files and `known_hosts` are not secrets.** They are public or integrity data. Recommend blocking the directory wholesale for simplicity, or excluding `*.pub` and `known_hosts` if path-specific control is wanted. |

### PEM, keys, and certificate material

The important distinction: **a certificate is public. A private key is not.**
`openssl x509` and `openssl req -pubkey` output are not secrets.

**Path-based blocking is imprecise here** because `.pem`, `.key`, and `.crt` are
all used for both. The correct test is content: does the file contain a
`PRIVATE KEY` block?

| Pattern | Why | False positives |
| --- | --- | --- |
| `*.key` | Conventional private key name. | Public keys are sometimes `.key` too. Common enough to flag but not common enough to block content-wise. |
| `*.p12`, `*.pfx` | PKCS#12 bundles. **Always contain private material or a key protected by one.** No false positive concern. | None. |
| `*.jks` | Java keystore. Same. | None. |
| `*.keystore` | Java keystore by another name. | Low. |
| `*.crt`, `*.cer` | **Deliberately NOT blocked.** Ordinary public certificates. | — |

**Recommended content check, independent of path.** Block a file if it contains
any of these PEM headers, regardless of its name:

```
-----BEGIN RSA PRIVATE KEY-----
-----BEGIN DSA PRIVATE KEY-----
-----BEGIN EC PRIVATE KEY-----
-----BEGIN OPENSSH PRIVATE KEY-----
-----BEGIN PRIVATE KEY-----            (PKCS#8)
-----BEGIN ENCRYPTED PRIVATE KEY-----
```

This catches private keys under any filename, which no path list can. It also
means `openssl` commands handling public certificates are unaffected.

A design note: reading a file to check its content means the check itself reads
the secret. It should happen in the enforcement layer, not be surfaced to the
agent, and the content must not be logged or returned.

### Cloud provider credentials

| Pattern | Why | False positives |
| --- | --- | --- |
| `~/.aws/credentials` | AWS shared credentials file. | None. |
| `~/.aws/config` | May contain account metadata. Not always secret. Recommend blocking; low cost. | Low — this file is rarely needed by an agent. |
| `~/.aws/**` | Catches everything under the AWS directory. | `~/.aws/cli/cache` is cache data. Recommend the directory-wide block for simplicity. |
| `~/.azure/**` | Azure CLI and MSAL token caches. | Low. |
| `~/.config/gcloud/**`, `~/.gcloud/**` | GCP application default credentials and tokens. | Low. |
| `credentials.json`, `service-account*.json` | GCP service account keys. **Mixed:** `service-account*.json` is structurally unambiguous (denied). Bare `credentials.json` is a name guess → **approval-required**. | `credentials.json` is a generic name. Split accordingly. |
| `application_default_credentials.json` | GCP ADC specifically. Format is unambiguous. | None. |
| `*.boto`, `*.gcloud/*.boto` | boto config, may hold keys. | Low. |
| `.terraformrc`, `terraform.rc` | May hold provider tokens. | Low. |

### Docker and registry auth

| Pattern | Why | False positives |
| --- | --- | --- |
| `~/.docker/config.json` | Contains registry auth, base64-encoded but reversible. | None. |
| `.dockercfg` | Legacy Docker auth file. | None. |
| `.netrc` | Machine-wide login credentials. Covered in its own section. | — |
| `~/.config/gh/hosts.yml` | GitHub CLI auth token. | None. |
| `~/.git-credentials` | Plaintext credentials written by `git credential-store`. | None. |
| `~/.npmrc`, `~/.pypirc`, `~/.cargo/credentials*`, `~/.gem/credentials` | Package registry tokens. | Low. Recommend `credentials` scoping rather than whole-file blocking where practical. |

### Git credential files

| Pattern | Why | False positives |
| --- | --- | --- |
| `~/.git-credentials` | Plaintext credential store. | None. |
| `~/.gitconfig` | Usually not secret; may contain credential helper config and URLs with embedded tokens. | **This is a judgement call.** Recommend blocking — the file an agent most likely needs is the one whose tokens are most likely embedded in fetch URLs. |
| `.git-credentials` in workspace | Same file, project-local. | None. |
| `.git/config` | May contain credential helpers and token-bearing URLs. | Low cost to block. |

Note: `git config --get` can read these. That is caught by the command list
(section 2, "credential inspection"), not by path matching.

### netrc

| Pattern | Why | False positives |
| --- | --- | --- |
| `.netrc` | Login/password triples for remote hosts. | None. |
| `_netrc` | Historical variant. | None. |
| `~/.netrc`, `~/.authinfo`, `~/.authinfo-gpg` | User-level equivalents. | None. |

### Kubernetes

| Pattern | Why | False positives |
| --- | --- | --- |
| `~/.kube/config` | Cluster credentials, embedded certs and tokens. | None. |
| `kubeconfig`, `kubeconfig.*`, `*.kubeconfig` | Non-default-named kubeconfigs. **Mixed:** the name is conventional but not proof — an application kubeconfig may be a non-secret sample. → **approval-required**, except under `~/.kube/` where location proves it. | Low — generic-ish name. |
| `*.kube/config` in workspace | Project-local. Name-guess → approval-required. | Low. |
| `k8s-secret*.yaml` | Manifests with inline secret data. Conventional name. | Low — but the name is specific enough to be evidence. Approved over denied. |

A kubeconfig under `~/.kube/config` is denied: location proves it. A kubeconfig
elsewhere is approval-required: the name suggests, the location does not prove.
An application kubeconfig with **no credentials** (only cluster address and
context) is not secret at all, and approval is the right treatment for it.

### Application secret files — name-guess, approval-required

These names suggest sensitivity but do not prove it. Per the principle, they are
**approval-required**, not denied.

| Pattern | Why approval-required rather than denied | False positives |
| --- | --- | --- |
| `secrets.yaml`, `secrets.yml`, `secrets.json` | Conventional name, but frequently sample data committed for documentation. | High — commonly non-secret. |
| `secret.json`, `secret.yaml` | Singular variants of the same. | High. |
| `*.secrets` | Generic name, no structural evidence. | High. |
| `credentials.json`, `credentials.yaml`, `credentials.yml` | Very common name for a non-secret schema or placeholder file. | High. |
| `*.password`, `*.token` | Suggestive name only. | Moderate. |
| `config.json` containing credential-looking keys | **Not a path pattern.** Detected by content key names, not filename. | Moderate. See below. |

The approval prompt must show the file path and size so the operator has
something to judge. "Approve reading `secrets.json`?" with no further
information is a prompt without information.

### Application secret files — structural, denied

These are denied because their structure or location proves they hold
credentials.

| Pattern | Why denied | False positives |
| --- | --- | --- |
| `.htpasswd` | HTTP basic auth hashes. Proven by the format. | None. |
| `*.keytab` | Kerberos keytab. Proven by the format. | None. |
| `vault-token`, `.vault-token` | HashiCorp Vault tokens. Format is unambiguous. | Low. |

### Content-key detection

Beyond filename and path, a file can be classified by **key names inside it**.
This is a distinct mechanism and worth separating, because it is stronger
evidence than a filename and weaker than a private-key block.

Keys whose presence indicates credential material: `api_key`, `apiKey`,
`secret`, `secret_key`, `secretKey`, `access_token`, `accessToken`,
`refresh_token`, `client_secret`, `private_key`, `password`, `passwd`,
`token`, `auth`, `authorization`, `credentials`.

**Recommended treatment: approval-required, not denied.** A `token` key in a
sample config is not a secret. Key names alone should not hard-deny.

Key names whose presence indicates genuine secret material regardless of file:
nothing beyond the `PRIVATE KEY` PEM headers, which stay denied.

Note: this content-key check requires reading the file, which means reading a
candidate that may be large or binary. It should be bounded — head of file
only, and text files only — so classification does not become an unbounded read.

### Local credential stores

Structural. Denied — the location or format proves what these hold.

| Pattern | Why denied | False positives |
| --- | --- | --- |
| `~/.password-store/**` | `pass` — encrypted, but entry names reveal secrets. | None. |
| `~/.gnupg/**` | GnuPG keyring. Private keys are exportable. | Low. Recommend denying. |
| `~/.local/share/keyrings/**` | Desktop keyring DBs. | Low. |
| `*.kdbx` | KeePass databases. Encrypted, but the location proves intent to store secrets. | None. |
| `*.1pif`, `*.agilekeychain` | 1Password exports. | None. |
| `**/Library/Keychains/**`, `**/.security/**` | macOS keychain. | None. |

**Deliberately not blocked:** password manager *vault* files. A `.kdbx` is
encrypted and safe; the syncing providers' own caches are not, and are covered
under the cloud patterns.

### Tiers at a glance

The list is split by evidence, not by convenience:

| Tier | Treatment | Examples |
| --- | --- | --- |
| Known credential directories | Denied | `~/.ssh/**`, `~/.aws/**`, `~/.kube/`, `~/.gnupg/**`, `~/.config/gh/` |
| Known credential stores | Denied | `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, `*.keytab` |
| Known private-key formats | Denied | `id_rsa`, `id_ed25519`, `*.ppk`, `*.pem` with private material |
| Known authentication files | Denied | `.netrc`, `_netrc`, `.authinfo`, `~/.git-credentials`, `~/.docker/config.json` |
| Files containing private-key material | Denied, any filename | PEM `PRIVATE KEY` headers |
| Structurally proven sources | Denied | `~/.kube/config`, `service-account*.json`, `.htpasswd`, `vault-token` |
| Ambiguous name-based patterns | **Approval-required** | `credentials.json`, `secrets.json`, `secrets.yaml`, `*.secrets`, `*.token`, `*.password` |
| Content key-name matches | **Approval-required** | files containing `api_key`, `client_secret`, `token` keys |

## List 2 — Known-dangerous command patterns

Never automatic. Either approval-required or denied outright.

Classification follows the rule already set in [byte-tools.md](byte-tools.md):
**denied** where a human being careful could not make it safe; **approval-
required** where a human might legitimately want it.

### Denied by default

| Operation | Pattern | Why denied rather than approved |
| --- | --- | --- |
| Force push | `git push --force`, `git push -f`, `git push --force-with-lease`, `git send-pack --force` | Destroys other people's commits. No legitimate need in v1. `--force-with-lease` is still a force push. |
| Destructive git history | `git reset --hard`, `git filter-branch`, `git filter-repo`, `git push --delete`, `git push origin :branch`, `git reflog expire`, `git gc --prune=now --aggressive`, `git checkout -- .`, `git clean -fdx` | Irreversible for anyone else. Recovering is possible only with help and only sometimes. |
> **`git rebase` is classified here as approval-required, not denied.** The
> original text denied it conditionally — "rewriting published history" — but
> whether a ref is published depends on the remote's state, which a static
> classifier cannot query. An unevaluable condition must not be treated as
> satisfied, so v1 gates every `git rebase` and leaves the judgement to the
> operator. See the rebase ruling in [byte-tools.md](byte-tools.md).

| Branch deletion | `git branch -D`, `git branch -d` on main/master, `git update-ref -d` | Deletes work, sometimes other people's. |
| sudo / su | `sudo`, `su`, `doas`, `pkexec`, `runuser` | System-level by definition. An agent that can become root has no meaningful permission model. |
| System service modification | `systemctl start/stop/restart/disable/enable/mask`, `service ...`, `launchctl load/unload`, `sc stop/start` | Changes machine state outside the workspace. Not recoverable by re-running an agent. |
| Shutdown / reboot / power state | `shutdown`, `reboot`, `poweroff`, `halt`, `init 0/6`, `systemctl poweroff/reboot`, `pmi` | Self-evidently outside what an agent should do. |
| Raw device writes | `dd if=... of=/dev/...`, `mkfs*`, `mkswap`, `fdisk`, `parted`, `sgdisk`, `wipefs`, `shred`, `blkdiscard`, `hpparm`, `dd of=/dev/nvme*`, `nvme format`, `diskutil eraseDisk` | Wipes data irrecoverably. Even with approval, this is not an agent operation. |
| Filesystem formatting | `mkfs.ext4`, `mkfs.btrfs`, `mkfs.ntfs`, `newfs`, `format` | Same as above. |
| Disk partitioning | `parted`, `fdisk`, `sgdisk`, `partprobe` | Can render a machine unbootable. |
| Out-of-workspace filesystem access | Any path argument resolving outside the workspace: `mv`, `cp`, `rm`, `chmod`, `chown`, `ln`, `truncate` | Denied by the workspace boundary, independent of the command list. |
| Permission / ownership changes outside workspace | `chmod`, `chown`, `chgrp`, `setfacl`, `umask` with a non-workspace path | Same boundary reason. Inside the workspace these are approval-required, see below. |
| Credential inspection | `env`, `printenv`, `set`, `cat .env`, `cat ~/.aws/credentials`, `aws configure list`, `gcloud auth print-access-token`, `gh auth token`, `docker login --password` | The agent would see a secret. This is the command-side counterpart to the secret-source list; needed because `env` reveals values without touching a file. |
| Environment dumping | `env`, `printenv`, `set`, `export -p`, `declare -x`, `compgen -e` | Same reason. `env` is the obvious one and it is trivially available to any agent. |
| Package installation | `pip install`, `uv pip install`, `poetry add`, `npm install`, `npm i`, `yarn add`, `pnpm add`, `apt install`, `apt-get install`, `dnf install`, `yum install`, `apk add`, `pacman -S`, `brew install`, `gem install`, `cargo install`, `go get`, `conda install` | Excluded from v1. Reaches a package index, runs third-party install code, and changes the environment every later command depends on. Not a v1 decision — see byte-tools.md. |
| System package management | `apt`, `dnf`, `yum`, `apk`, `pacman`, `brew`, `snap`, plus any with root | System-level. |
| Fork bomb / resource exhaustion | `:(){ :|:& };:`, while-loop resource traps | Self-DoS. Cheap to match, permanently damaging. |
| History and log tampering | `history -c`, `rm ~/.bash_history`, `> /var/log/...`, `truncate -s 0` on logs | Removes the audit trail. Ties to the audit question in byte-tools.md. |
| Raw disk and filesystem inspection writes | `chattr`, `lsattr`, `setfattr`, `debugfs`, `tune2fs`, `resize2fs`, `e2fsck`, `xfs_repair` | Can corrupt a filesystem. |
| Device and kernel access | `modprobe`, `insmod`, `rmmod`, `sysctl -w`, writing under `/proc`, `/sys`, `/dev` | System-level. |
| Network and firewall changes | `iptables`, `nft`, `ufw`, `firewall-cmd`, `ip link set`, `route` modification, `systemctl` networking | Changes machine connectivity. |
| User and group management | `useradd`, `usermod`, `groupadd`, `passwd`, `chpasswd`, `visudo`, `/etc/sudoers` edits | System-level. |

### Approval-required

| Operation | Pattern | Why approval rather than denial |
| --- | --- | --- |
| Recursive deletion inside workspace | `rm -r`, `rm -rf`, `rm --recursive`, `find ... -delete`, `find ... -exec rm`, `shutil.rmtree` | `rm -rf` on a stale build directory is routine. The targets are known and inspectable. Approval with the resolved target list displayed makes this safe. |
| Recursive deletion with a glob | `rm -rf *`, `rm -rf ./*`, `rm -rf build/*`, `rm -rf **/*.tmp`, any unresolved wildcard in a recursive delete | **Denied in v1.** See the wildcard section below. |
| Explicit recursive deletion of known paths | `rm -rf build`, `rm -rf ./dist`, `rm -rf node_modules` — named targets inside the workspace | Approval-required. The targets are known and shown in the prompt. |
| Overwrite / truncate | `> file`, `truncate`, `shred`, `dd of=<file>` | Destructive but scoped and knowable. `shred` and `dd` appear in both tables; v1 splits them by target — a device target is denied as raw erasure, an ordinary file target is approval-required. |
| Bulk rewrite in workspace | `sed -i`, `find -exec sed`, in-place rewrites across many files | Legitimate refactors. The diff is knowable. |
| Git commit | `git commit`, `git commit -a`, `git merge` creating a commit | The point is human judgement of the message and intent. Already policy. |
| Git push (non-force) | `git push` | Recoverable locally, but publishes. Already policy. |
| Git branch / checkout / merge / rebase / stash / tag | `git branch`, `git switch`, `git checkout`, `git merge`, `git rebase`, `git stash`, `git tag` | Everyday development. Not excluded from v1 as tools — available through approval. Note: byte-tools.md lists these as "not implemented in v1" as tools; reaching them through `exec_command` is a separate question. |
| `git fetch` / `git pull` | `git fetch`, `git pull` | `git pull` can merge and can conflict. `git fetch` alone is safe but shares the pattern. |
| chmod / chown inside workspace | `chmod`, `chown`, `chgrp`, `setfacl` with in-workspace paths | Legitimate, scoped, reversible. |
| Broad chmod / chown | `chmod -R`, `chmod 777`, `chown -R`, `chown` on paths outside workspace (denied elsewhere) | `-R` and `777` are the flags worth catching. |
| Process termination | `kill`, `pkill`, `killall`, `kill -9`, `pkill -9` | Killing a runaway test is legitimate. Killing something unrelated is not. |
| Process termination outside workspace context | `kill` targeting a process not owned by the current run | Distinguishing "my child process" from "someone's server" is the whole question. See uncertainties. |
| Database destructive commands | `DROP DATABASE`, `DROP TABLE`, `DROP SCHEMA`, `TRUNCATE`, `DELETE FROM` without `WHERE`, `psql -c "DROP"`, `mysql -e`, `TRUNCATE TABLE` | Genuinely destructive. But database work is legitimate, so approval with the statement shown is right — **if** the database is reachable at all in v1, which is undecided. |
| `git config` writes | `git config --global`, `git config --system`, `git config --unset` | Changes machine-wide git behaviour. |
| `chattr` attribute changes | `chattr +i`, `chattr -i` | Can make files unmodifiable, which breaks the agent's own ability to work. |
| Network calls that cost money or leave the machine | `curl`, `wget`, `http`, `nc`, paid API calls, `mail`, `sendmail` | Read-only fetches are fine; the issue is egress and cost. **Recommendation: fetch tools are approval-required in v1 rather than automatic**, since egress is unbounded. |
| Global git operations on refs | `git update-ref`, `git symbolic-ref` | Ref manipulation is powerful and rarely benign. |
| Docker/containers | `docker run`, `docker exec`, `docker stop`, `docker system prune` | `docker run` can mount the host filesystem. |
| Crontab | `crontab -e`, writing to `/etc/cron*` | Scheduling is n8n's job per architecture.md. An agent scheduling itself contradicts the design. |

## List 3 — Automatic operations

**Decision: automatic is an explicit rule.** An operation is automatic only
because a rule in this list says so. It is not automatic because nothing was
recognised. This list is the allowlist; the absence of a rule is
approval-required (see [Unknown commands](#unknown-commands)).

> **Decision: v1 ships a deliberately small allowlist.**
>
>     if we are not confident it belongs in automatic,
>     leave it at APPROVAL_REQUIRED
>
> The governing principle, and the reason this list is short rather than
> comprehensive. Broadening it to reduce prompt count would trade a safety
> property for convenience, which is the trade this design refuses to make
> everywhere else. A missing row costs one prompt; a wrongly-added row costs a
> silent execution. That asymmetry sets the size of the table.
>
> Coverage is expected to grow over time, one reviewed row at a time, and each
> addition is a normal change to this file. Nothing requires the list to be
> exhaustive for the baseline to be correct.

Each entry is narrow on purpose. The table below is grouped by family, but the
granularity that matters is **program plus operation**, not program alone — see
the git rows for cases where the same program has automatic and
approval-required forms.

### Read-only inspection, inside the workspace

The inspection tools Byte needs to do its job. All are read-only, none can
mutate the workspace, and none is a repository-code executor.

| Operation | Examples |
| --- | --- |
| List a directory | `ls`, `pwd` |
| Search filenames | `find` (read forms only), `search_files` |
| Search contents | `grep`, `rg`, `search_content` |
| Read a file | `cat`, `head`, `tail` |
| Measure and identify | `wc`, `stat`, `file` |
| Compare two files | `diff` |
| Version probes | `--version` on any recognised program |

`find` is automatic in its read forms (`-name`, `-path`, `-type`, `-print`).
`find ... -delete` and `find ... -exec rm` are destructive and are
approval-required or denied per the wildcard policy below.

**Not automatic, despite being inspection-shaped.** `du` and `df` are omitted:
`df` reports mounted filesystems outside the workspace, which is host
information Byte has no v1 need for. `tree`, `less`, and `more` are omitted as
inconsistent rather than dangerous — a later row if wanted. `which`, `type`,
`date`, `uname`, and `id` are omitted because v1 has no use for them; they can
be added on review.

**`env`, `printenv`, and `set` are DENIED**, per List 2. There is no name-only
form in v1. Reading a variable by name rather than by value is a genuinely
different operation, and the classifier has no rule shape capable of
structurally distinguishing the two — so the safe reading is the denied one. No
matcher capability will be added for this in the current implementation pass.

### Git inspection

Automatic forms only:

| Operation | Example |
| --- | --- |
| Working tree state | `git status` |
| Change inspection | `git diff`, `git diff --cached` |
| History | `git log`, `git show` |
| Object and ref reads | `git rev-parse`, `git ls-files` |
| Branch listing | `git branch` with no mutation flag, `git branch --list` |
| Tag listing | `git tag` with no mutation flag, `git tag --list` |
| Stash listing | `git stash list` |

Everything else in the git family is approval-required or denied per List 2.
**`git branch`, `git tag`, and `git stash` are not one permission level each.**
Each has an automatic listing form and an approval-required mutating form, and
they are classified per operation:

| Command | Classification |
| --- | --- |
| `git branch`, `git branch --list`, `git branch -a -v` | Automatic |
| `git branch -d`, `-D` | **Denied** |
| `git branch <name>`, `git branch -c <name>`, `git switch <name>` | Approval-required |
| `git tag`, `git tag --list` | Automatic |
| `git tag -d <name>` | **Denied** (destroys work, same reasoning as branch deletion) |
| `git tag <name>`, `git tag -a` | Approval-required |
| `git stash list` | Automatic |
| `git stash push`, `pop`, `drop`, `clear` | Approval-required (`drop`/`clear` destroy state) |

### Structured Forge tools

Not command invocations, and listed here because the permission model is about
operations rather than about `exec_command` alone.

| Tool | Condition |
| --- | --- |
| `read_file` | In-workspace, not a secret source |
| `write_file` | In-workspace, not a secret source |
| `patch_file` | In-workspace, not a secret source |
| `search_files`, `search_content` | In-workspace |

**A secret source is never automatic regardless of the tool.** A name-guess
secret read through `read_file` is approval-required, because the secret verdict
is ranked together with the command verdict and the strictest wins.

## Unknown commands

**Decision: an unrecognised argv invocation is APPROVAL_REQUIRED.** This
replaces the earlier "convenience case", which classified anything unmatched as
automatic.

### The model

```
    explicit DENY rules      -> denied
    explicit APPROVAL rules  -> approval-required
    explicit ALLOW rules     -> automatic
    nothing matched          -> approval-required
```

Strictest-wins is unchanged and still resolves the result across every rule that
fires. What changed is the default, and that an ALLOW rule table now exists to
be matched against.

### Why

The failure mode of a classifier miss is what matters. With unknown-as-automatic,
a miss means **silent execution** — Forge runs something it never classified. The
entire conservatism argument in this document depends on a miss degrading to a
prompt instead, and it did not.

Approval-required rather than denied, because denying the unknown would refuse a
large number of legitimate development commands an operator would happily
approve. A refusal teaches the operator to look for a way around the tool; a
prompt costs them one keystroke and keeps them in the loop. Byte is documented as
a permitted agent, and prompts are the price of that.

The cost is honest and worth stating: **an unfamiliar toolchain now produces a
prompt per command**, and unattended runs against a toolchain that is not fully
covered by List 3 will fail closed on ordinary work. That is the correct
behaviour under the fail-closed rule, and it is a real operational cost rather
than a hypothetical one.

The mitigation is structural rather than procedural: List 3 is *data*, so
covering a new program is adding a row, not changing code. And the table is
additive in the safe direction — a missing row costs one prompt, never an
unreviewed execution.

Note what this fixed incidentally. `git branch`, `git stash`, `git tag`, and
`git checkout` are listed under approval-required in List 2 and previously
matched no rule at all, so they silently classified as automatic. Under this
model they classify as the spec says they should.

## Workspace-local executables

**Decision: an executable that resolves inside the current workspace is
APPROVAL_REQUIRED.**

### The rule

Resolve the program token to a real path. If that path is inside the workspace
root, the operation is approval-required. If it resolves outside, containment
denies it as any other out-of-workspace path.

This is a question about *where the code comes from*, not about the shape of the
token:

| Program token | Classification |
| --- | --- |
| `./configure` | Approval-required — resolves inside the workspace |
| `./scripts/check.sh` | Approval-required — resolves inside the workspace |
| `./tool` | Approval-required — resolves inside the workspace |
| `/workspace/bin/lint` | Approval-required — resolves inside the workspace |
| `../outside/tool` | **Denied** — resolves outside the workspace |
| `/usr/bin/git` | Classified by its normal program and operation rules |
| `/usr/bin/python` | Classified by its normal program and operation rules |

**A path separator in the token is not the test.** `/usr/bin/git` contains one
and is an ordinary system program. The test is where the resolved path lands.
A workspace-local executable is arbitrary code the agent chose to run; a system
program is a named, reviewed piece of the host.

### Why this is not inconsistent with trusting `git`

A repository is attacker-reachable input. Cloning a project is importing code,
and the workspace is therefore not a trust boundary in the way `/usr/bin` is. A
file's *location* is not evidence of its behaviour: the executable bit is the
only signal available, and it is a signal the operator set rather than one Forge
can verify.

This is the same argument that gates `cmd.inline_code`, and the two are kept
consistent on purpose. `./scripts/check.sh` is arbitrary code in exactly the
sense `python -c "..."` is: the form permits anything. Gating inline code while
trusting a workspace-local executable because of where it lives would be
incoherent.

A prompt is sufficient. The path and arguments are short and readable, and the
operator can open the file before answering. This is a stop-and-look gate of the
kind `cmd.inline_code` already is.

### The v2 deferral

A repository may later declare named workflow commands that are trusted
automatically. That mechanism is **v2 and is not implemented or configured in
v1** — see [Project-declared workflow commands](#project-declared-workflow-commands).
When it exists, a workspace-local executable does not become automatic through
it: the declaration narrows approval-required operations, and an executable that
resolves inside the workspace is gated because of where it came from, not
because of what it was asked to do.

## Repository code is not a trust boundary

**Decision: commands that execute repository code are APPROVAL_REQUIRED in v1.**

| Operation | Form |
| --- | --- |
| `pytest`, `pytest tests/ -q` | Approval-required |
| `python -m pytest` | Approval-required |
| `make`, `make test`, `make build` | Approval-required |
| `npm test`, `npm run <script>` | Approval-required |
| `cargo test`, `cargo build`, `cargo run` | Approval-required |
| `go test`, `go build`, `go run` | Approval-required |
| `tox`, `nox` | Approval-required |

**Why this is a change from the earlier draft.** That draft called running tests
automatic, on the reasoning that tests "may legitimately do things that look
destructive" and that prompting on them would make the agent useless. Both
halves are true and neither settles the question, because the draft also gated
`./scripts/check.sh` — a repository shell script — while trusting `pytest`
automatically. Those two positions cannot both hold.

`pytest` is not a passive reader of test files. It executes `conftest.py`, loads
installed plugins, imports fixtures, and runs arbitrary test code — all of it
repository-supplied. So a `pytest` invocation is repository code executing by
another name, and it belongs in the same bucket as
`./scripts/check.sh`. This is the workspace-trust argument from the previous
section, applied to a program that happens to have a familiar name.

The prompt cost is real and is accepted: an agent that verifies its work will
ask before each test run. That is the correct trade under a model whose central
claim is that a miss becomes a prompt.

**Reading a test file is not this.** `cat tests/test_thing.py`, `grep`,
`search_content`, and `read_file` on a test file are read-only inspection and
remain automatic per List 3. The gate is on *executing* repository code.

**This is a policy default, not a permanent verdict.** A reviewed project
workflow mechanism (v2) may narrow selected approval-required operations to
automatic. That is the intended way to reduce the prompt cost without
reintroducing a trust shortcut. No such mechanism exists in v1.

## Project-declared workflow commands

**Decision: deferred to v2. No configuration format, no code, and no config file
in v1.**

A repository will eventually need to declare its normal commands — `make test`,
`npm test`, `cargo test`, `./scripts/check.sh` — so that an agent working in it
is not prompted for the project's entire test cycle. That is a real need. It is
not a v1 capability.

### Why not v1

- It is the first mechanism in Forge that would move an operation from
  approval-required to automatic **without a human present at the time**. Every
  other v1 gate is either a code-level rule or a live prompt.
- The declarations live in the repository, which is the untrusted input the
  workspace-trust argument is about. A repository declaring its own permissions
  inverts the trust direction.
- Its value partly comes from avoiding curation of List 3. It is not a
  substitute for it.

### Constraints recorded for v2

So the mechanism is not designed afresh later:

1. A declaration may narrow **approval-required → automatic only**. It may never
   affect a denial, and it may never apply to a workspace-local executable, to
   inline code, to a shell string, or to network egress.
2. It is a static data file in the repository, reviewed through the normal
   code-review path.
3. Entries are program plus exact argv prefix, matched with the same structured
   matcher used everywhere else. No globs, no shell, no `=`.
4. `./scripts/check.sh` is **not eligible** to become automatic through this
   mechanism.
5. Whether the mechanism is available at all is a Forge-level operator setting,
   not a per-repository switch, so a repository cannot opt itself in.

## Workspace boundary

Status: **decided for v1.**

- Each Byte runtime has **exactly one explicit workspace root**. Not a default,
  not an inferred location — an explicit value that is part of the runtime's
  configuration.
- Automatic file and command operations must resolve **inside that root**.
- Symlinks and `..` are resolved **before** the containment check, never after.
- Any resolved target outside the workspace root is **denied**.
- Multi-project and named workspace sets are **deferred to a later version**.

### Rationale

One root, not a set. A set of allowed paths is a list to get wrong, and every
entry is a place containment can leak. A single root has one containment
comparison, which is the one check the classifier can rely on absolutely.

This is a real limitation and is accepted knowingly: Byte works in one project
per runtime. Multi-project work means either a workspace root that contains
several projects, or the deferred named-set design. Neither is a v1 concern.

Deferring the set also defers the interesting question — whether a path in one
allowed root may reference another — rather than answering it badly now.

### Consequences

- A path that does not exist yet still resolves lexically and is checked.
- Symlink loops and broken symlinks fail closed, not open.
- A path argument that is not a path (a bare flag, a message) is not a
  containment subject and is not checked.

## Wildcard and unresolved-target policy

**Decision: destructive filesystem operations containing unresolved globs or
wildcard targets are denied in v1.**

### The rule

A destructive operation whose affected targets cannot be enumerated before
execution is **denied**, not approval-gated.

Denied, all of these:

```
rm -rf *
rm -rf ./*
rm -rf build/*
rm -rf **/*.tmp
rm -r src/*
find . -name '*.tmp' -delete
find . -name '*.log' -exec rm {} \;
```

Still approval-required, all of these:

```
rm -rf build
rm -rf ./dist
rm -rf node_modules
rm -rf build/output tests/fixtures
```

Still denied regardless, both because they leave the workspace and because they
are recursive:

```
rm -rf /tmp/*
rm -rf ../sibling/*
```

### Why deny rather than prompt

An approval prompt exists so the operator can evaluate a specific action against
what they can see. `rm -rf *` offers nothing to evaluate. The prompt would read:

```
Approve: rm -rf * in /home/user/projects/the_forge ?
Affected targets: *
```

An operator approving this is not making a decision. They are clicking through a
control that has stopped being a control, and worse, they have just granted
permission for a deletion whose extent is defined by whatever happened to be in
that directory at that moment — which may include files the agent was about to
work on, or files that were never meant to be deleted.

The prompt would also be lying by omission: it displays a field labelled
"affected targets" containing a glob. An operator who has learned to read that
field would correctly conclude the operation is small.

### The required alternative

**Byte must resolve the wildcard itself and request deletion with explicit
targets.**

The flow Forge requires:

1. Byte enumerates what the glob would match, using the file search tools it
   already has.
2. Byte requests the deletion with those resolved paths listed explicitly.
3. Forge classifies the resulting command as a known-target recursive delete:
   approval-required.
4. The approval prompt shows the actual target list, and the operator approves
   that list.

This is not a workaround. It is a better outcome: the resolved-target prompt is
strictly more informative than the glob version, and the agent is forced to
demonstrate what it intends to delete before asking. An agent that was going to
sweep away too much now has to name it.

It also preserves the real workflow. Cleaning a build directory is still one
command away — Byte just has to say which directory, or enumerate it first.

### Detection

Unresolved wildcard characters in any path argument of a destructive command:
`*`, `?`, `[...]`, `{...}`, and shell-uncached `~` expansion. Detection is on
the argument string before any expansion, since the point is that Forge is not
expanding it.

Two consequences worth noting:

- A literal filename containing `*` is vanishingly rare and will be denied. An
  operator who needs it can rename the file.
- `--` does not help here, and neither does quoting. Quoting `*` does not make
  the target list knowable; it just means the shell would have expanded it.

### Future

Per-project glob allowlists are a possible future capability. They are not in
v1, and v1 should not be built in anticipation of them — the current design
already gives Byte a path to the same result.

### Process killing

**Decision: a process may be killed automatically only when Forge can prove it
started that process as part of the current run.**

Killing your own runaway test needs no prompt. Killing a database server
somebody else is running needs one. Forge distinguishes these by provenance, not
by inspection: it knows which process IDs it created during this run, and a kill
target is in that set or it is not.

| Target | Treatment |
| --- | --- |
| A PID in the current run's owned set | **Automatic** |
| Any other PID | **Approval-required** |
| System-wide or privileged process control | **Denied** |
| `killall`, `pkill` patterns without an owned-PID match | **Approval-required** |
| Signals against PID 1, or `-u root` / `--user root` | **Denied** |

Provenance is the only reliable test. Checking whether a process "looks like"
one of ours — a name match, a parent PID guess — is the same class of guess the
command classifier avoids everywhere else. A PID Forge spawned, then recycled by
the OS to an unrelated process, is a theoretical hole here; v1 accepts it in
exchange for not guessing.

`killall` and `pkill` match by pattern rather than PID, so they cannot be
resolved to an owned process. They are approval-required, and requesting them
by pattern while owning the target is not a supported path in v1.

### Database destructive commands

**Decision: include recognizable destructive database operations in v1
classification, even though Forge has no database connectivity yet.**

The rules ship now so that the classifier is complete when connectivity arrives,
and so that the decision is reviewed while it is cheap to change.

| Form | Treatment |
| --- | --- |
| `DROP DATABASE`, `DROP SCHEMA` | Approval-required |
| `DROP TABLE` | Approval-required |
| `TRUNCATE [TABLE]` | Approval-required |
| `DELETE FROM <t>` without `WHERE` | Approval-required |
| `DELETE FROM <t>` with `WHERE` | Automatic |
| `ALTER`/`UPDATE` with no `WHERE` | Approval-required |
| Ambiguous SQL reaching a database client | Approval-required |

**Forge does not parse SQL.** It does not parse shell, and the same reasoning
applies: `DELETE FROM t` and `DROP /* comment */ TABLE` are separated by string
inspection that will eventually be wrong. Classification is keyword presence
over the argument text.

**Ambiguous SQL defaults to approval-required if it enters a destructive-looking
path.** A statement reaching a database client that is not clearly safe to
classify is gated, never automatic.

Note the asymmetry with the command tables: destructive SQL is
approval-required rather than denied, because the operator can read the
statement in the prompt. `DROP DATABASE` against a database they did not choose
is a mistake an operator can catch.

### Commands that escape workspace boundaries

This is not a command pattern but a path check, and it is listed here because
the two interact.

Rule: **any** path argument resolving outside the workspace is denied, whatever
the command is. `cp /etc/passwd ./x` is denied. `cat ../../secrets` is denied.
`python -c "open('/etc/shadow')"` is **not** caught by this rule — see
uncertainties.

The workspace check runs first, before command classification, because it is
the one input Forge can rely on.

## How each pattern should be evaluated

### Matching structure

For a structured invocation, build a match record:

```
{
  program: str,          # basename, lowercased
  args: list[str],       # all arguments
  flags: set[str],       # short and long flags extracted from args
  path_args: list[Path], # arguments that resolve to existing or creatable paths
  shell: bool
}
```

Then:

1. If `shell` is true → **approval-required**. Do not parse. Do not attempt
   partial parsing to "improve" the result — partial parsing that reports "safe"
   is worse than no parsing.
2. Resolve every path argument. If any resolves outside the workspace →
   **denied**. Stop.
3. Resolve the program token itself. If it resolves inside the workspace →
   **approval-required** (see [Workspace-local executables](#workspace-local-executables)).
4. If any resolved path matches a secret-source pattern → **denied**. Stop.
5. Match program and flags against the command tables. Take the strictest
   match: denied > approval-required > automatic.
6. If nothing matched → **approval-required**. Never automatic by default. See
   [Unknown commands](#unknown-commands).
7. Report the matched pattern name with the decision, so a denial is
   explainable rather than mysterious.

Step 7 matters operationally. A denied operation that says which rule denied it
is fixable; one that says "permission error" is not. The same applies to step 6:
an approval-required decision should name *why* it was ambiguous, or the operator
is being asked to approve something Forge could not describe.

### Which patterns cause approval vs denial

The rule, as already stated in byte-tools.md: **denied** where a human being
careful could not make it safe; **approval-required** where a human might
legitimately want it.

- Root, raw devices, filesystem destruction, irreversible history rewrite,
  secret values, system state, and package installation → denied.
- Everything inside the workspace that a developer might reasonably do today →
  approval-required.

### How ambiguous commands are handled

- `shell: true` → always approval-required. Non-negotiable in v1.
- **An unrecognised argv invocation → approval-required.** This replaces the
  earlier convenience case. The reasoning, and the contradiction it resolved in
  this document's own summary, are in [Unknown commands](#unknown-commands).
- Anything whose *targets cannot be determined before execution* → **denied**
  under the wildcard policy above. Not approval-gated.
- A tool that would let Byte supply arbitrary code → **approval-required**,
  always. These are shell strings wearing a disguise, and matching them as argv
  would be a false sense of safety.

That last point deserves emphasis. `python -c "import os; os.system('rm -rf /')"`
has argv that matches nothing dangerous. It should be approval-required because
of what the *form* allows, not what the *arguments* say.

The workspace-local executable rule is the same principle applied to a
different form. `./scripts/check.sh` is not a shell string, but it is arbitrary
code by the same reasoning, and it is gated for the same reason.

### Inline-code execution

**Decision: inline-code execution is approval-required in v1. Forge does not
attempt to determine whether the inline source is safe.**

Approval-required, all of these and their equivalents:

```
python -c "..."        python3 -c "..."      pypy -c "..."
bash -c "..."          sh -c "..."           zsh -c "..."
node -e "..."          perl -e "..."         ruby -e "..."
php -r "..."           deno eval "..."       bun -e "..."
```

**Forge does not parse, inspect, or analyse the inline source.** Not with a
sandbox, not with a denylist of functions, not with static analysis. The reason
is that it does not work in the direction that matters: `os.system`, `subprocess`,
`eval`, `exec`, `open().write()`, and `__import__` are enough to express
anything, and each is one alias or one indirection away from defeating any list
of them. A checker that reports inline code as safe would be wrong often enough
to be dangerous, and right rarely enough to be useless.

The approval is therefore not about *what the code does*. It is a
**stop-and-look** gate: the operator sees that the agent asked to run arbitrary
code, and can read the source in the prompt — the code is short enough to read
by eye in nearly every legitimate case.

This is blunt on purpose. It also means Byte cannot run a one-line Python
helper without a prompt, which is a real usability cost accepted for v1.

### Network egress

**Decision: general-purpose network commands remain approval-required in v1, and
arbitrary shell network access does not become Byte's web interface.**

Approval-required:

```
curl ...                wget ...
http ...                httpie ...
nc / ncat / netcat      openssl s_client
ssh ...                 scp ...   sftp ...
git clone / git fetch / git pull
```

`ssh` and `scp` are listed here because they are network egress *and* they are
a common route to credentials on the remote host.

### What this is not

Byte does **not** get an implicit web interface through `curl`. The reasoning:
`curl` takes a URL and inherits whatever the host network allows — internal
addresses, cloud metadata endpoints, arbitrary ports, arbitrary protocols. An
automatic-approval `curl` is an SSRF primitive pointed at whatever infrastructure
the Forge host sits next to, and it is also unbounded egress with unbounded cost.

### The future path

A **dedicated Forge network/web tool** may be designed later, with its own
permissions and constraints — a URL allowlist, content-type and size limits,
redaction, and approval for anything outside the obvious cases. That is a
separate design and is not part of v1.

Until it exists, web access is a deliberate gap: Byte reads the workspace and
runs its tools, and a human fetches what it cannot. That is a limitation, not
an oversight, and it is preferable to a shell escape hatch that doubles as
unrestricted network access.

### Platform scope

**Command classification v1 is Linux-focused.**

The command tables in List 2, the secret-source path patterns in List 1, the
workspace path model, and the interpreter and egress patterns above are all
written for a Linux host with a POSIX shell environment.

**Windows and PowerShell classification are explicitly outside the v1
classifier.** No `del /s`, no `Remove-Item -Recurse`, no PowerShell equivalents,
no Windows credential store paths, no `%APPDATA%` layouts, no `attrib`.

### Why the core still allows for Windows

The architecture does not prevent a future Windows policy. Nothing in the
design assumes POSIX: classification matches on a structured `{program, args,
cwd}` record, and the classifier is data, not code branches through a shell. A
Windows policy is a second dataset plus a different path-resolution strategy,
loaded alongside the Linux one or selected by host platform.

It is simply not written, because writing untested Windows patterns would mean
shipping rules nobody has verified against a real Windows host — which is worse
than having no rules and saying so.

**Forge v1 targets Linux. Running on Windows is unsupported in v1 and the
classifier will not protect a Windows host.**

## Items I am unsure about

**Resolved decisions.** Five items previously raised here are now settled and
documented above: name-guess secret patterns are approval-required rather than
denied (see the principle section), wildcard recursive deletes are denied
(see the wildcard policy), inline-code execution is approval-required with no
source analysis, network commands are approval-required with no dedicated
web tool in v1, and **an unrecognised argv invocation is approval-required
rather than automatic** (see [Unknown commands](#unknown-commands)). Platform
scope is settled too: Linux-focused, Windows out. Two further decisions were
added during the permission-model pass: executables resolving inside the
workspace are approval-required ([Workspace-local
executables](#workspace-local-executables)), and commands that execute
repository code are approval-required ([Repository code is not a trust
boundary](#repository-code-is-not-a-trust-boundary)).

What remains is not a list of decisions blocking implementation. These are
judgement calls, noted because a reviewer should see them rather than discover
them later.

1. **Process killing scope.** I list `kill`/`pkill` as approval-required, which
   means Byte cannot terminate its own runaway test without a prompt. That is
   probably correct for v1 and will be annoying in practice. Deciding "safe to
   kill my own descendants without asking" would need a process-tree check.

2. **Database commands.** Listed as approval-required, but whether Byte can
   reach a database in v1 is undecided. If it cannot, this whole subsection is
   premature. If it can, then `DELETE FROM t WHERE 1=1` needs catching too, and
   string-matching SQL against a multi-line `-c` argument is a rabbit hole.

3. **`~/.gitconfig` and `~/.aws/config`.** Both classified structural-denied.
   Neither is strictly secret, but both are token-bearing in common
   configurations. The reasoning is that the file an agent most needs is the one
   most likely to leak. Could be over-blocking.

4. **Content-based private key detection.** The `PRIVATE KEY` header check is
   the only thing that reliably separates private from public material, and it
   works on names. It also means the enforcement layer reads secrets — it must
   never log or return what it reads. That constraint needs honouring in the
   implementation, not just noted here.

5. **Content-key detection cost.** Reading a file head to check for
   credential-looking keys adds an I/O read to every classification. Bounded to
   text files and a head slice, but it is still a read that could fail on
   permissions or disappear mid-check. Failure behaviour needs deciding: fail
   closed, or treat as clean?

6. **Aliases, functions, and wrapper scripts.** `rm` may not be `/bin/rm` on the
   host. Matching on basename handles common cases and misses the rest. A known
   limitation, stated rather than hidden.

7. **Overlapping patterns.** `~/.kube/config` is denied by location while
   `*.kube/config` elsewhere is approval-required. A path can match both a
   structural and a name pattern, and the strictest-wins rule resolves it, but
   the interaction should be tested rather than assumed.

8. **Path resolution for paths that do not exist yet.** `rm -rf build` where
   `build` does not exist. The check must handle non-existent paths without
   either failing open or rejecting a legitimate delete of something already
   gone.

9. **The convenience-case risk — RESOLVED, and the resolution was the opposite
   of what this item assumed.** "A program not on either list, invoked with
   argv → automatic" was called "the single largest soft spot in the whole
   design", and narrowing it "would mean allowlisting rather than blocklisting,
   which changes the character of the classifier substantially". It does change
   the character's character, and the change was accepted: the classifier is now
   an explicit three-table model with an approval-required default. See
   [Unknown commands](#unknown-commands). The cost is stated there too — prompt
   per unrecognised command, and unattended runs failing closed on a toolchain
   List 3 does not cover.

10. **Scope of List 3 at launch — RULED: deliberately small.** Because the
    default is approval-required, a program missing from the automatic table
    costs a prompt on every invocation. Ruled that v1 ships a short list under
    the principle "if we are not confident it belongs in automatic, leave it at
    APPROVAL_REQUIRED", and that the list is **not** broadened to reduce prompt
    count. Coverage grows one reviewed row at a time.

11. **`git add` batching — RULED: per invocation.** One `git add` is one
    approval decision, whatever it stages. No per-path approval or batching
    semantics in v1; that would be a UX refinement of the same gate.

12. **Unattended runs against a partial List 3 — ACCEPTED.** An unattended run
    reaching an unknown or unapproved command fails closed. This is intentional
    and follows directly from ruling that a miss must produce a prompt, applied
    where nobody can answer one. No unattended auto-approval and no persistent
    command trust. The operational precondition is stated rather than hidden: an
    unattended run is viable only for a project whose toolchain is fully covered
    by List 3.

13. **`env` / `printenv` / `set` — RULED: stay denied.** There is no name-only
    environment read in v1. The classifier has no rule shape capable of
    structurally distinguishing a safe by-name form from a value-dumping one, so
    the denied reading stands until a matcher capability exists to make the
    distinction structural. No such capability will be added in the current
    implementation pass.

## Summary for implementation

Secret-source patterns in two tiers. **Structural or content-proven → denied**
with no approval path: known credential directories, credential stores,
private-key formats, authentication files, PEM `PRIVATE KEY` headers, and
similar. **Filename merely suggestive → approval-required**: `credentials.json`,
`secrets.json`, `secrets.yaml`, `*.secrets`, `*.token`, `*.password`, and
credential-looking key names found in content.

Ordinary public certificates are not blocked.

Known-dangerous commands: **denied** for root, raw devices, filesystem
destruction, history rewrite, secret values, system state, and package
installation. **Approval-required** for in-workspace development operations,
inline-code execution, network egress, repository-code execution (tests and
build runners), and any executable resolving inside the workspace.

**Automatic is an explicit rule** (List 3): read-only inspection, git inspection
in its listing forms, and Byte's structured in-workspace file tools. Reading a
file is automatic; executing what the repository contains is not.

Any invocation using a shell string → **approval-required**, never parsed.

**An unrecognised argv invocation → approval-required.** Automatic is never
the default. A classifier miss produces a prompt, not silent execution.

Destructive operations with unresolved wildcards → **denied**. Byte resolves the
target list itself and re-requests with explicit paths.

Platform scope: **Linux only in v1.** Windows and PowerShell patterns are out of
scope and the classifier will not protect a Windows host.

**This is a conservative classifier, not a sandbox.** It does not contain
processes, isolate filesystems, or stop a determined operator. It raises the
cost of accident, which is the actual goal.