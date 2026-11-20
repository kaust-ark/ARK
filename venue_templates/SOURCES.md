# Submission template sources

Checked on 2026-11-20. `main.tex` in each directory is the Idea2Paper writing scaffold. The linked kits supply the venue styles and bibliography formats. SHA-256 values identify the downloaded archives. The ACL and TMLR rows pin the upstream Git commit.

| Template | Edition | Review and update notes | Official sources |
| --- | --- | --- | --- |
| ICML | 2027 | Updated review style, bibliography and supporting files. The skeleton includes impact and AI use statements. | [Guidelines](https://icml.cc/Conferences/2027/CallForPapers) · [Kit](https://media.icml.cc/Conferences/ICML2027/styles/icml2027.zip) |
| ICLR | 2027 | Updated anonymous style, bibliography and AI use statement. | [Guidelines](https://iclr.cc/Conferences/2027/AuthorGuidelines) · [Kit](https://media.iclr.cc/Conferences/ICLR2027/iclr-2027-style-files.zip) |
| NeurIPS | 2026 | Updated the 2026 style and its 16-question checklist. The checklist follows the appendix. The scaffold omits the author instructions marked for removal and retains all questions. | [Guidelines](https://neurips.cc/Conferences/2026/CallForPapers) · [Kit](https://media.neurips.cc/Conferences/NeurIPS2026/Formatting_Instructions_For_NeurIPS_2026.zip) |
| MLSys | 2027 call, 2025 format | The call explicitly requires the 2025 kit. All five style files are unchanged. Submit appendices separately. | [Guidelines](https://mlsys.org/Conferences/2027/CallForResearchPapers) · [Kit](https://media.mlsys.org/Conferences/MLSYS2025/mlsys2025style.zip) |
| AAAI | 2027.1 | Updated anonymous style and bibliography. The reproducibility checklist is a separate submission file. | [Guidelines](https://aaai.org/conference/aaai/aaai-27/submission-instructions/) · [Kit](https://aaai.org/wp-content/uploads/2026/10/AuthorKit27.zip) |
| ACL | 2026-06-29 | Updated the author-list formatting in the bibliography. The review style is unchanged. | [Guidelines](https://acl-org.github.io/ACLPUB/formatting.html) · [Commit `d5adc823f`](https://github.com/acl-org/acl-style-files/tree/d5adc823ff0f80f98c80405ca0ab66c68e684409) |
| CVPR | 2027 v1 | Updated review style. The skeleton includes the required LLM Usage Statement before references. | [Guidelines](https://cvpr.thecvf.com/Conferences/2027/AuthorGuidelines) · [Kit](https://media.eventhosts.cc/Conferences/CVPR2027/author-kit-CVPR2027-v1-latex.zip) |
| ACM SIGPLAN | acmart 2.21 | Updated class and bibliography. The shared SIGPLAN skeleton enables anonymous review. | [Package](https://ctan.org/pkg/acmart) · [Kit](https://ctan.math.illinois.edu/macros/latex/contrib/acmart.zip) |
| IEEE conferences | IEEEtran 1.8b | The class and BibTeX style match the release byte for byte. | [Guidelines](https://www.ieee.org/conferences/publishing/templates.html) · [Kit](https://ctan.math.illinois.edu/macros/latex/contrib/IEEEtran.zip) |
| USENIX | 2020-09 style in Security 2027 kit | The shared style matches the 2027 kit byte for byte. The skeleton adopts its URL-aware bibliography style. | [Guidelines](https://www.usenix.org/conference/usenixsecurity27/call-for-papers) · [Kit](https://www.usenix.org/sites/default/files/usenixsecurity2027_latex_templates.zip) |
| TMLR | 2023-06-30 | The three style files match the maintainer’s latest revision. The distribution now includes its Apache license. | [Guidelines](https://www.jmlr.org/tmlr/author-guide.html) · [Commit `7bf90efe3`](https://github.com/JmlrOrg/tmlr-style-file/tree/7bf90efe3a0debbba703c05c43f3ff7e4d4a2992) |
| Generic article | Standard LaTeX | The installed TeX distribution supplies the class. The scaffold is unchanged. | [Package](https://www.latex-project.org/get/) · [Standard classes](https://ctan.org/pkg/classes) |

## Archive checksums

Run `sha256sum` on the downloaded ZIP to compare it with the recorded value. For ACM, `acmart.cls` comes from running `tex -8bit -no-shell-escape acmart.ins` alongside the upstream `acmart.dtx`. The result matches the TeX Live class byte for byte. The source accompanies the generated class under its distribution notice.

| Kit | SHA-256 |
| --- | --- |
| ICML | `52384a2a4f32e562755e44ced55a58f857f8706425f2f04904190f613531801e` |
| ICLR | `0d940dfa9398ae99a18f24a85a8a683f367204b6af6d17d2899e60a67102529e` |
| NeurIPS | `82473931e3ef710fcd3f4a8cd4119b9de32e56825f90f9e5a6d55f2d01b817d9` |
| MLSys | `04e77090038f78c985154c71f7a57b7fbb2553ddd3e4e62f431ed420ba3768ef` |
| AAAI | `9406a545d5d213757245eb37118fe652b2fccf64f555aff28b49ebe2a780c960` |
| CVPR | `da1ec5db9b73531ea82a87723e4aedb631083331fe655d1972279b48a7bdb2fe` |
| ACM SIGPLAN | `81977a834f5cfd5302c37181ede34d2663eeea60604d1d89230e3c5e74b9a4bb` |
| IEEE conferences | `e0cd4f5afbd42c8076092280e72b3e0a5111efe501d35de9f715cfb8da313cb4` |
| USENIX | `7dc55fc858881a7d64a06d7c8277be25c6dcc6070b17f74ef0d8a6bed70aa38e` |
