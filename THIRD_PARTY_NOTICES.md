# Third-party notices

EternalChat is released under the MIT License (LICENSE). Some of its design and
parts of its code are adapted from other projects, under their licenses.

## UniiChat

<https://github.com/VictorTaelin/uniichat> · Copyright 2026 Higher Order Company ·
Apache License 2.0 (<https://www.apache.org/licenses/LICENSE-2.0>)

Adapted, in Python, with changes:

- the memory design: the summary tree, its view and zoom (the UniiChat design
  gist, revision 3c190e06f34aba0c69f49042c526093269604935);
- the computer's tools (`eternalchat/machine.py`, from `Machine.ts`): their
  contract, descriptions and diff answers;
- the web page's layout of the chat as its memory (`eternalchat/web.html`, after
  `web/style.css` and `web/src/Html.ts`), redrawn in EternalChat's own style.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
those parts except in compliance with the License. Unless required by applicable
law or agreed to in writing, software distributed under the License is
distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the specific language governing
permissions and limitations under the License.
