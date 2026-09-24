### Uh oh!

There was an error while loading. Please reload this page.

[openai](/openai) 
/
**[openai-python](/openai/openai-python)**
Public

* [Notifications](/login?return_to=%2Fopenai%2Fopenai-python) You must be signed in to change notification settings
* [Fork
  5.2k](/login?return_to=%2Fopenai%2Fopenai-python)
* [Star
   31.6k](/login?return_to=%2Fopenai%2Fopenai-python)

# v1.0.0 Migration Guide #742

Locked

[rattrayalex](/rattrayalex)
started this conversation in
[General](/openai/openai-python/discussions/categories/general)

[v1.0.0 Migration Guide](#top)
#742

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=40&v=4)

rattrayalex](/rattrayalex)

Nov 8, 2023
·
73 comments
·
207 replies

[Return to top](#top)

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

## [rattrayalex](/rattrayalex) [Nov 8, 2023](#discussion-5829467)

|  |
| --- |
| We have released a new major version of our SDK, and we recommend upgrading promptly.  It's a total rewrite of the library, so many things have changed, but we've made upgrading easy with a code migration script and detailed docs below. It was extensively [beta tested](https://github.com/openai/openai-python/discussions/631) prior to release. Getting started ``` pip install --upgrade openai ``` What's changed  * Auto-retry with backoff if there's an error * Proper types (for mypy/pyright/editors) * You can now instantiate a client, instead of using a global default. * Switch to explicit client instantiation * Weights and Biases CLI will now be included in their own package  Migration guide *For Azure OpenAI users, see [Microsoft's Azure-specific migration guide](https://aka.ms/oai/v1-python-migration).* Automatic migration with grit You can automatically migrate your codebase using [grit](https://grit.io/), either [online](https://app.grit.io/migrations/new/openai) or with the following CLI command on Mac or Linux:  ``` openai migrate ```  The grit binary executes entirely locally with AST-based transforms.  Be sure to audit its changes: we suggest ensuring you have a clean working tree beforehand, and running `git add --patch` afterwards. Note that grit.io also offers opt-in automatic fixes powered by AI. Automatic migration with grit on Windows To use grit to migrate your code on Windows, you will need to use Windows Subsystem for Linux (WSL). [Installing WSL](https://learn.microsoft.com/en-us/windows/wsl/install) is quick and easy, and you do not need to keep using Linux once the command is done.  Here's a step-by-step guide for setting up and using WSL for this purpose:   1. Open a PowerShell or Command Prompt as an administrator and run `wsl --install`. 2. Restart your computer. 3. Open the WSL application. 4. In the WSL terminal, `cd` into the appropriate directory (e.g., `cd /mnt/c/Users/Myself/my/code/`) and then run the following commands: ```    curl -fsSL https://docs.grit.io/install | bash    grit install    grit apply openai    ```   Then, you can close WSL and go back to using Windows. Automatic migration with grit in Jupyter Notebooks If your Jupyter notebooks are not in source control, they will be more difficult to migrate. You may want to copy each cell into grit's web interface, and paste the output back in.  If you need to migrate in a way that preserves use of the module-level client instead of instantiated clients, you can use [the openai\_global grit migration](https://app.grit.io/migrations/new/openai_global) instead. Initialization ``` # old import openai  openai.api_key = os.environ['OPENAI_API_KEY']  # new from openai import OpenAI  client = OpenAI(   api_key=os.environ['OPENAI_API_KEY'],  # this is also the default, it can be omitted ) ``` Responses Response objects are now [pydantic models](https://docs.pydantic.dev/latest/usage/models/) and no longer conform to the dictionary shape. However you can easily convert them to a dictionary with `model.model_dump()`.  ``` # before import json import openai  completion = openai.Completion.create(model='curie') print(completion['choices'][0]['text']) print(completion.get('usage')) print(json.dumps(completion, indent=2))  # after from openai import OpenAI  client = OpenAI()  completion = client.completions.create(model='curie') print(completion.choices[0].text) print(dict(completion).get('usage')) print(completion.model_dump_json(indent=2)) ``` Async client We do not support calling asynchronous methods in the module-level client, instead you will have to instantiate an async client.  The rest of the API is exactly the same as the synchronous client.  ``` # old import openai  completion = openai.ChatCompletion.acreate(model="gpt-3.5-turbo", messages=[{"role": "user", "content": "Hello world"}])  # new from openai import AsyncOpenAI  client = AsyncOpenAI() completion = await client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "user", "content": "Hello world"}]) ``` Module client Important  We highly recommend instantiating client instances instead of relying on the global client.  We also expose a global client instance that is accessible in a similar fashion to versions prior to v1.  ``` import openai  # optional; defaults to `os.environ['OPENAI_API_KEY']` openai.api_key = '...'  # all client options can be configured just like the `OpenAI` instantiation counterpart openai.base_url = "https://..." openai.default_headers = {"x-foo": "true"}  completion = openai.chat.completions.create(     model="gpt-4",     messages=[         {             "role": "user",             "content": "How do I output all files in a directory using Python?",         },     ], ) print(completion.choices[0].message.content) ```  The API is the exact same as the standard client instance based API.  This is intended to be used within REPLs or notebooks for faster iteration, **not** in application code.  We recommend that you always instantiate a client (e.g., with `client = OpenAI()`) in application code because:   * It can be difficult to reason about where client options are configured * It's not possible to change certain client options without potentially causing race conditions * It's harder to mock for testing purposes * It's not possible to control cleanup of network connections  Pagination All `list()` methods that support pagination in the API now support automatic iteration, for example:  ``` from openai import OpenAI  client = OpenAI()  for job in client.fine_tuning.jobs.list(limit=1):     print(job) ```  Previously you would have to explicitly call a `.auto_paging_iter()` method instead.  See the [README](https://github.com/openai/openai-python/tree/main/README.md) for more details. Azure OpenAI To use this library with [Azure OpenAI](https://learn.microsoft.com/en-us/azure/ai-services/openai/overview), use the `AzureOpenAI` class instead of the `OpenAI` class.  A more comprehensive [Azure-specific migration guide is available on the Microsoft website](https://aka.ms/oai/v1-python-migration).  Important  The Azure API shape differs from the core API shape which means that the static types for responses / params  won't always be correct.  ``` from openai import AzureOpenAI  # gets the API Key from environment variable AZURE_OPENAI_API_KEY client = AzureOpenAI(     # https://learn.microsoft.com/en-us/azure/ai-services/openai/reference#rest-api-versioning     api_version="2023-07-01-preview",     # https://learn.microsoft.com/en-us/azure/cognitive-services/openai/how-to/create-resource?pivots=web-portal#create-a-resource     azure_endpoint="https://example-endpoint.openai.azure.com", )  completion = client.chat.completions.create(     model="deployment-name",  # e.g. gpt-35-instant     messages=[         {             "role": "user",             "content": "How do I output all files in a directory using Python?",         },     ], ) print(completion.model_dump_json(indent=2)) ```  In addition to the options provided in the base `OpenAI` client, the following options are provided:   * `azure_endpoint` * `azure_deployment` * `api_version` * `azure_ad_token` * `azure_ad_token_provider`   An example of using the client with Azure Active Directory can be found [here](https://github.com/openai/openai-python/tree/main/examples/azure_ad.py). All name changes Note: all `a*` methods have been removed; the [async client](#async-client) must be used instead.   * `openai.api_base` -> `openai.base_url` * `openai.proxy` -> `openai.proxies` ([docs](https://www.python-httpx.org/compatibility/#proxy-keys)) * `openai.InvalidRequestError` -> `openai.BadRequestError` * `openai.Audio.transcribe()` -> `client.audio.transcriptions.create()` * `openai.Audio.translate()` -> `client.audio.translations.create()` * `openai.ChatCompletion.create()` -> `client.chat.completions.create()` * `openai.Completion.create()` -> `client.completions.create()` * `openai.Edit.create()` -> `client.edits.create()` * `openai.Embedding.create()` -> `client.embeddings.create()` * `openai.File.create()` -> `client.files.create()` * `openai.File.list()` -> `client.files.list()` * `openai.File.retrieve()` -> `client.files.retrieve()` * `openai.File.download()` -> `client.files.retrieve_content()` * `openai.FineTune.cancel()` -> `client.fine_tunes.cancel()` * `openai.FineTune.list()` -> `client.fine_tunes.list()` * `openai.FineTune.list_events()` -> `client.fine_tunes.list_events()` * `openai.FineTune.stream_events()` -> `client.fine_tunes.list_events(stream=True)` * `openai.FineTune.retrieve()` -> `client.fine_tunes.retrieve()` * `openai.FineTune.delete()` -> `client.fine_tunes.delete()` * `openai.FineTune.create()` -> `client.fine_tunes.create()` * `openai.FineTuningJob.create()` -> `client.fine_tuning.jobs.create()` * `openai.FineTuningJob.cancel()` -> `client.fine_tuning.jobs.cancel()` * `openai.FineTuningJob.delete()` -> `client.fine_tuning.jobs.create()` * `openai.FineTuningJob.retrieve()` -> `client.fine_tuning.jobs.retrieve()` * `openai.FineTuningJob.list()` -> `client.fine_tuning.jobs.list()` * `openai.FineTuningJob.list_events()` -> `client.fine_tuning.jobs.list_events()` * `openai.Image.create()` -> `client.images.generate()` * `openai.Image.create_variation()` -> `client.images.create_variation()` * `openai.Image.create_edit()` -> `client.images.edit()` * `openai.Model.list()` -> `client.models.list()` * `openai.Model.delete()` -> `client.models.delete()` * `openai.Model.retrieve()` -> `client.models.retrieve()` * `openai.Moderation.create()` -> `client.moderations.create()` * `openai.api_resources` -> `openai.resources`  Removed  * `openai.api_key_path` * `openai.app_info` * `openai.debug` * `openai.log` * `openai.OpenAIError` * `openai.Audio.transcribe_raw()` * `openai.Audio.translate_raw()` * `openai.ErrorObject` * `openai.Customer` * `openai.api_version` * `openai.verify_ssl_certs` * `openai.api_type` * `openai.enable_telemetry` * `openai.ca_bundle_path` * `openai.requestssession` (we now use [httpx](https://github.com/encode/httpx)) * `openai.aiosession` (we now use [httpx](https://github.com/encode/httpx)) * `openai.Deployment` (only used for Azure) – please use the [azure-mgmt-cognitiveservices](https://pypi.org/project/azure-mgmt-cognitiveservices) client library instead (here's [how to list deployments](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/list_deployments.py), for example). * `openai.Engine` * `openai.File.find_matching_files()` * `openai.embeddings_utils` (now in the cookbook) |

31
 You must be logged in to vote

👍
86
  👎
24
  😄
4
  🎉
11
  😕
4
  ❤️
9
  🚀
8
  👀
14

 
All reactions

* 👍
  86
* 👎
  24
* 😄
  4
* 🎉
  11
* 😕
  4
* ❤️
  9
* 🚀
  8
* 👀
  14

## Replies: 73 comments · 207 replies

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [jakobdylanc](/jakobdylanc) [Nov 8, 2023](#discussioncomment-7516101)

|  |
| --- |
| Is the highlighted line a typo? (image removed because it was huge) |

6
 You must be logged in to vote

👍
1

 
All reactions

* 👍
  1

2 replies

[![@RobertCraigie](https://avatars.githubusercontent.com/u/23125036?s=60&v=4)](/RobertCraigie)

#### [RobertCraigie](/RobertCraigie) [Nov 9, 2023](#discussioncomment-7516816)

|  |
| --- |
| Thanks, fixed! |

All reactions

[![@TSSFL](https://avatars.githubusercontent.com/u/33649199?s=60&v=4)](/TSSFL)

#### [TSSFL](/TSSFL) [Jan 16, 2024](#discussioncomment-8148825)

|  |
| --- |
| Which code is the new equivalent to the old one here <https://tssfl.com/viewtopic.php?t=6651> ? |

All reactions

### [hammad93](/hammad93) [Nov 9, 2023](#discussioncomment-7517565)

|  |
| --- |
| Needs a comma after defining `api_version` in the snippet,  ``` client = AzureOpenAI(     # https://learn.microsoft.com/en-us/azure/ai-services/openai/reference#rest-api-versioning     api_version="2023-07-01-preview"     # https://learn.microsoft.com/en-us/azure/cognitive-services/openai/how-to/create-resource?pivots=web-portal#create-a-resource     azure_endpoint="https://example-endpoint.openai.azure.com", ) ``` |

8
 You must be logged in to vote

All reactions

1 reply

[![@RobertCraigie](https://avatars.githubusercontent.com/u/23125036?s=60&v=4)](/RobertCraigie)

#### [RobertCraigie](/RobertCraigie) [Nov 9, 2023](#discussioncomment-7520397)

|  |
| --- |
| Thanks, fixed! |

All reactions

### [giopoi](/giopoi) [Nov 9, 2023](#discussioncomment-7522425)

|  |
| --- |
| Incomprensibile ma chi le scrive queste cose? |

6
 You must be logged in to vote

All reactions

2 replies

[![@RobertCraigie](https://avatars.githubusercontent.com/u/23125036?s=60&v=4)](/RobertCraigie)

#### [RobertCraigie](/RobertCraigie) [Nov 9, 2023](#discussioncomment-7522550)

|  |
| --- |
| Which parts did you find difficult to understand? Do you have any feedback on where we could improve the guide? |

All reactions

[![@skyl](https://avatars.githubusercontent.com/u/61438?s=60&v=4)](/skyl)

#### [skyl](/skyl) [Dec 16, 2023](#discussioncomment-7869373)

|  |
| --- |
| I followed the author because I thought it was such a good effort 🤷‍♀️ |

All reactions

### [giopoi](/giopoi) [Nov 9, 2023](#discussioncomment-7522445)

|  |
| --- |
| You can run `openai migrate` to automatically upgrade your codebase to use the 1.0.0 interface.  in windows non funziona |

9
 You must be logged in to vote

All reactions

17 replies

Show 12 previous replies

[![@AveryTang11](https://avatars.githubusercontent.com/u/92308566?s=60&v=4)](/AveryTang11)

#### [AveryTang11](/AveryTang11) [Dec 5, 2023](#discussioncomment-7760725)

|  |
| --- |
| now there is another problem |

All reactions

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Dec 5, 2023](#discussioncomment-7766233)

|  |
| --- |
| Hi @TeddyTang11, the correct command is `grit apply openai`. |

All reactions

[![@AveryTang11](https://avatars.githubusercontent.com/u/92308566?s=60&v=4)](/AveryTang11)

#### [AveryTang11](/AveryTang11) [Dec 6, 2023](#discussioncomment-7770486)

|  |
| --- |
| OK, i'll try it. Thanks very much! |

All reactions

[![@shivv-001](https://avatars.githubusercontent.com/u/156197124?s=60&v=4)](/shivv-001)

#### [shivv-001](/shivv-001) [Feb 7, 2024](#discussioncomment-8397539)

|  |  |
| --- | --- |
| | i'm facing thefollowing issue could you please help me with that | | --- |   raise APIRemovedInV1(symbol=self.\_symbol)  openai.lib.\_old\_api.APIRemovedInV1:  You tried to access openai.Completion, but this is no longer supported in openai>=1.0.0 - see the README at <https://github.com/openai/openai-python> for the API.  You can run `openai migrate` to automatically upgrade your codebase to use the 1.0.0 interface.  Alternatively, you can pin your installation to the old version, e.g. `pip install openai==0.28`  A detailed migration guide is available here: [#742](https://github.com/openai/openai-python/discussions/742) |

😕
5

 
All reactions

* 😕
  5

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 7, 2024](#discussioncomment-8397554)

|  |
| --- |
| Use this <https://platform.openai.com/docs/api-reference/images> |

All reactions

### [stevechappell2000](/stevechappell2000) [Nov 9, 2023](#discussioncomment-7523244)

|  |
| --- |
| What happened to the functions array that was in chatCompletions? Seems to no longer function in the chat.completions?? Sample code?? |

7
 You must be logged in to vote

👍
3

 
All reactions

* 👍
  3

3 replies

[![@RobertCraigie](https://avatars.githubusercontent.com/u/23125036?s=60&v=4)](/RobertCraigie)

#### [RobertCraigie](/RobertCraigie) [Nov 9, 2023](#discussioncomment-7523251)

|  |  |  |
| --- | --- | --- |
| It's still there, what's not working for you?    [openai-python/src/openai/resources/chat/completions.py](https://github.com/openai/openai-python/blob/8e4e5d490629a5abfbdf4b6a2ce20c7eee848a2e/src/openai/resources/chat/completions.py#L63)  Line 63 in [8e4e5d4](/openai/openai-python/commit/8e4e5d490629a5abfbdf4b6a2ce20c7eee848a2e)  |  |  | | --- | --- | |  | functions: List[completion\_create\_params.Function] | NotGiven = NOT\_GIVEN, | |

All reactions

[![@stevechappell2000](https://avatars.githubusercontent.com/u/32968652?s=60&v=4)](/stevechappell2000)

#### [stevechappell2000](/stevechappell2000) [Nov 9, 2023](#discussioncomment-7523520)

|  |
| --- |
| I see some comment about functions has been replaced by tools? do you have a working sample? |

👍
1

 
All reactions

* 👍
  1

[![@phongvants123](https://avatars.githubusercontent.com/u/64645010?s=60&v=4)](/phongvants123)

#### [phongvants123](/phongvants123) [Feb 10, 2024](#discussioncomment-8430183)

|  |
| --- |
| did you figure it out, i have the same problem |

All reactions

### [giopoi](/giopoi) [Nov 9, 2023](#discussioncomment-7523349)

|  |
| --- |
| Adesso sono impegnato poi mi spieghero' Il gio 9 nov 2023, 15:39 Robert Craigie \*\*\*@\*\*\*.\*\*\*> ha scritto: […](#) It's still there, what's not working for you? <https://github.com/openai/openai-python/blob/8e4e5d490629a5abfbdf4b6a2ce20c7eee848a2e/src/openai/resources/chat/completions.py#L63> — Reply to this email directly, view it on GitHub <[#742 (reply in thread)](https://github.com/openai/openai-python/discussions/742#discussioncomment-7523251)>, or unsubscribe <<https://github.com/notifications/unsubscribe-auth/AAMXYQRJPKWOUMVS7OXZSVTYDTTKRAVCNFSM6AAAAAA7DOKUK2VHI2DSMVQWIX3LMV43SRDJONRXK43TNFXW4Q3PNVWWK3TUHM3TKMRTGI2TC>> . You are receiving this because you commented.Message ID: \*\*\*@\*\*\*.\*\*\*> |

1
 You must be logged in to vote

All reactions

0 replies

### [ppravdin](/ppravdin) [Nov 9, 2023](#discussioncomment-7524641)

|  |
| --- |
| It would be great if you could update ChatGPT's knowledge as well. Currently, it provides OpenAI-related code of an old version, even when I provide examples and ask for updates to the new version. |

21
 You must be logged in to vote

All reactions

6 replies

Show 1 previous reply

[![@RobertCraigie](https://avatars.githubusercontent.com/u/23125036?s=60&v=4)](/RobertCraigie)

#### [RobertCraigie](/RobertCraigie) [Nov 10, 2023](#discussioncomment-7534188)

|  |
| --- |
| [@ppravdin](https://github.com/ppravdin) you should be able to still pin the version you're installing in google colab, `pip install openai==0.28` |

👍
4

 
All reactions

* 👍
  4

[![@golean1](https://avatars.githubusercontent.com/u/83517404?s=60&v=4)](/golean1)

#### [golean1](/golean1) [Nov 27, 2023](#discussioncomment-7686067)

|  |
| --- |
| Are there any solutions to getting chatgpt to strictly reply with the new API update code? I seem to be having the same issue therer and in the assistant playground. Even when you attach or send the api documention to it, it seems to only hold that context for a few responses at most before regressing back to the old code. It's been a bummer to not be able to get as much help from chatgpt on the assistant api for example compared to anything else from april and prior. |

👍
2

 
All reactions

* 👍
  2

[![@kenhuangus](https://avatars.githubusercontent.com/u/1869785?s=60&v=4)](/kenhuangus)

#### [kenhuangus](/kenhuangus) [Dec 30, 2023](#discussioncomment-7980042)

|  |
| --- |
| The best way is not to introduce break changes in SDK. Instead fix the old SDK to redirect calls via API to new SDK. So, people use old SDK prior to version 1.00 can still use the old SDK unless they want to leverage new functionality in new SDK. This should really be a responsibility of SDK provider or developer to make sure the burden of break fix in on their side especially if there are many users of SDK. OpenAI can be a pioneer in this and promise no break changes in the future. Technically it is doable, you just need to put more development efforts into it. But, if you train your LLM to change your old SDK to redirect calls to new SDK your efforts is minimum. |

All reactions

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Dec 30, 2023](#discussioncomment-7980992) Author

|  |
| --- |
| Breaking changes between major versions, especially pre-1.0 versions, is standard practice in software development. Therefore, OpenAI chose not to prioritize the development effort required for full backwards compatibility with v0.28.x, which had many shortcomings.  [@kenhuangus](https://github.com/kenhuangus) if you'd like to fork the v0.28.1 branch to rely on the v1.0 library, you're certainly welcome to, and I'm sure you'd find users for it. |

👍
1

 
All reactions

* 👍
  1

[![@FlyingFathead](https://avatars.githubusercontent.com/u/86568670?s=60&v=4)](/FlyingFathead)

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

#### [FlyingFathead](/FlyingFathead) [Jan 3, 2024](#discussioncomment-8006723)

|  |
| --- |
| Should anyone need a snippet of working chatbot code for the `1.6.1` version of `openai` pip package; <https://github.com/FlyingFathead/TelegramBot-OpenAI-API/blob/b5c315e8dc46109279659a3adf6a997791cdbe35>  Just a forewarning -- that code is WIP, been building it the past week or so. But, it works pretty well, although using `httpx.AsyncClient()` seemed to have been the only properly working method I could come up with in terms of getting the payload just right and everything to work without issues.  If you need GPT-4 to assist you in figuring out the changes, I could suggest giving a snippet like that to it and asking to figure out what has changed in terms of the request and how it's formed with the newer `openai` library version.  Good luck! |

All reactions

### [jtoh89](/jtoh89) [Nov 9, 2023](#discussioncomment-7524922)

|  |
| --- |
| Was there a heads-up on this? We haven't deployed our tools utilizing openai api into production yet, but good thing we didn't. We had no idea that there would be a new version. Maybe next time there could be extended support for legacy? |

9
 You must be logged in to vote

👍
5

 
All reactions

* 👍
  5

12 replies

Show 7 previous replies

[![@richlysakowski](https://avatars.githubusercontent.com/u/9345711?s=60&v=4)](/richlysakowski)

#### [richlysakowski](/richlysakowski) [Dec 2, 2023](#discussioncomment-7736672)

|  |
| --- |
| Thoughtless Slippery Sam just threw out a truckloads of banana peels for the developers everywhere to slip and fall.  Really thoughtless. 1000s of developers have to rewrite their code with no thought to train the LLMs themselves to help the developers ahead of the release of so many breaking changes. |

👍
8
  ❤️
1

 
All reactions

* 👍
  8
* ❤️
  1

[![@endolith](https://avatars.githubusercontent.com/u/58611?s=60&v=4)](/endolith)

#### [endolith](/endolith) [Jan 8, 2024](#discussioncomment-8049147)

|  |
| --- |
| And they included a "migrate" tool that only migrates half of the code and leaves the rest broken. 😒 |

👍
4

 
All reactions

* 👍
  4

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 10, 2024](#discussioncomment-8074885) Author

|  |
| --- |
| [@endolith](https://github.com/endolith) can you please share a gist with examples of code that was not fully migrated, so we can fix the migration tool? (cc [@morgante](https://github.com/morgante)) |

All reactions

[![@endolith](https://avatars.githubusercontent.com/u/58611?s=60&v=4)](/endolith)

#### [endolith](/endolith) [Jan 10, 2024](#discussioncomment-8081763)

|  |
| --- |
| [@rattrayalex](https://github.com/rattrayalex) [#742 (reply in thread)](https://github.com/openai/openai-python/discussions/742#discussioncomment-8006276) |

👍
1

 
All reactions

* 👍
  1

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 10, 2024](#discussioncomment-8083836) Author

|  |
| --- |
| Thank you, sorry we missed that! |

All reactions

### [gojira](/gojira) [Nov 9, 2023](#discussioncomment-7525272)

|  |
| --- |
| [@rattrayalex](https://github.com/rattrayalex) [@RobertCraigie](https://github.com/RobertCraigie) - hey there - Keiji from Azure OpenAI here!  We have written a more extended Azure OpenAI migration guide in our documentation: <https://aka.ms/oai/v1-python-migration>  Would it be possible to add it to the main body of this Guide? |

2
 You must be logged in to vote

👍
3
  ❤️
1

 
All reactions

* 👍
  3
* ❤️
  1

4 replies

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 9, 2023](#discussioncomment-7527504) Author

|  |
| --- |
| Awesome, thank you [@gojira](https://github.com/gojira)! added. |

🎉
1
  ❤️
1

 
All reactions

* 🎉
  1
* ❤️
  1

[![@gojira](https://avatars.githubusercontent.com/u/137710?s=60&v=4)](/gojira)

#### [gojira](/gojira) [Nov 9, 2023](#discussioncomment-7527875)

|  |
| --- |
| Excellent thank you! |

👍
1

 
All reactions

* 👍
  1

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 10, 2023](#discussioncomment-7528199) Author

|  |
| --- |
| By the way, we're working with the team at Grit to add auto-migration for Azure. |

🎉
1

 
All reactions

* 🎉
  1

[![@gojira](https://avatars.githubusercontent.com/u/137710?s=60&v=4)](/gojira)

#### [gojira](/gojira) [Nov 10, 2023](#discussioncomment-7528305)

|  |
| --- |
| Oh sweet! That's epic! We'd be happy to test it. |

🎉
1

 
All reactions

* 🎉
  1

### This comment has been hidden.

[Sign in to view](/login?return_to=https%3A%2F%2Fgithub.com%2Fopenai%2Fopenai-python%2Fdiscussions%2F742)

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 9, 2023](#discussioncomment-7527206) Author

|  |
| --- |
| Sorry if our docs were unclear! I know there are a lot of technical terms in there. You might try pasting them into ChatGPT and asking it to clarify in Italian. |

😕
1

 
All reactions

* 😕
  1

### [giopoi](/giopoi) [Nov 9, 2023](#discussioncomment-7526380)

|  |
| --- |
| II don't want to use WSL Il giorno gio 9 nov 2023 alle ore 18:09 Alex Rattray < \*\*\*@\*\*\*.\*\*\*> ha scritto: […](#) You need to use Windows Subsystem for Linux (WSL) for this. <https://learn.microsoft.com/en-us/windows/wsl/install> — Reply to this email directly, view it on GitHub <[#742 (reply in thread)](https://github.com/openai/openai-python/discussions/742#discussioncomment-7525034)>, or unsubscribe <<https://github.com/notifications/unsubscribe-auth/AAMXYQQCMXDSAJLB5HT2WYTYDUE25AVCNFSM6AAAAAA7DOKUK2VHI2DSMVQWIX3LMV43SRDJONRXK43TNFXW4Q3PNVWWK3TUHM3TKMRVGAZTI>> . You are receiving this because you commented.Message ID: \*\*\*@\*\*\*.\*\*\*>    -- \*Giovanni Poidomani\* |

2
 You must be logged in to vote

All reactions

2 replies

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 9, 2023](#discussioncomment-7527191) Author

|  |
| --- |
| If you aren't able to use WSL to install `grit` to help migrate your code automatically, you can migrate your code by hand by following the guide above, or continue to use the older SDK with `pip install openai==0.28.0` if you don't need to upgrade. |

All reactions

[![@maxkellerya](https://avatars.githubusercontent.com/u/57807512?s=60&v=4)](/maxkellerya)

### This comment was marked as off-topic.

[Sign in to view](/login?return_to=https%3A%2F%2Fgithub.com%2Fopenai%2Fopenai-python%2Fdiscussions%2F742)

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [exanni](/exanni) [Nov 9, 2023](#discussioncomment-7526903)

|  |
| --- |
| still don't understand how to use grit |

1
 You must be logged in to vote

All reactions

1 reply

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 9, 2023](#discussioncomment-7527208) Author

|  |
| --- |
| What is the difficulty you're running into? The docs are here: <https://docs.grit.io/>  Note that if you're on Windows, you'll need to use WSL (Windows Subsystem for Linux).  cc [@morgante](https://github.com/morgante) in case there are follow-up questions |

All reactions

### [abi](/abi) [Nov 9, 2023](#discussioncomment-7527725)

|  |
| --- |
| Did openai.Timeout get changed to openai.APITimeoutError? Don't see that in the list of name changes. |

5
 You must be logged in to vote

👍
1

 
All reactions

* 👍
  1

5 replies

[![@zekesarosi](https://avatars.githubusercontent.com/u/87879722?s=60&v=4)](/zekesarosi)

#### [zekesarosi](/zekesarosi) [Nov 9, 2023](#discussioncomment-7527898)

|  |
| --- |
| Also it seems like you can't set a timeout parameter in a Chat Completions request anymore which is very annoying |

All reactions

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 10, 2023](#discussioncomment-7528146) Author

|  |
| --- |
| [@zekesarosi](https://github.com/zekesarosi) what gives you that impression? It's `client.chat.completions.create(model=…, messages=…, timeout=30)` |

❤️
1

 
All reactions

* ❤️
  1

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 10, 2023](#discussioncomment-7528182) Author

|  |
| --- |
| [@abi](https://github.com/abi) can you link to the old `openai.Timeout`? |

All reactions

[![@zekesarosi](https://avatars.githubusercontent.com/u/87879722?s=60&v=4)](/zekesarosi)

#### [zekesarosi](/zekesarosi) [Nov 10, 2023](#discussioncomment-7529128)

|  |
| --- |
| apologies [@rattrayalex](https://github.com/rattrayalex)  you are correct the timeout param still works, for some reason an app that I was running in the past all of a sudden started throwing errors about timeout being an invalid parameter. It was an error on my end and I didn't investigate enough before commenting. |

👍
1
  ❤️
1

 
All reactions

* 👍
  1
* ❤️
  1

[![@abi](https://avatars.githubusercontent.com/u/23818?s=60&v=4)](/abi)

#### [abi](/abi) [Nov 10, 2023](#discussioncomment-7536779)

|  |
| --- |
| [@rattrayalex](https://github.com/rattrayalex) See Timeout here: <https://platform.openai.com/docs/guides/error-codes/python-library-error-types> |

All reactions

### [TheSoldiersDream](/TheSoldiersDream) [Nov 10, 2023](#discussioncomment-7528501)

|  |
| --- |
| I am stuck with a bunch of bots that no longer work |

3
 You must be logged in to vote

😄
2

 
All reactions

* 😄
  2

8 replies

Show 3 previous replies

[![@NguyenGPhuc](https://avatars.githubusercontent.com/u/39476247?s=60&v=4)](/NguyenGPhuc)

#### [NguyenGPhuc](/NguyenGPhuc) [Nov 12, 2023](#discussioncomment-7546331)

|  |
| --- |
| Having the exact same problem. I was able to connect and get a response before with the old codes. This newer one makes no sense to me. Where exactly I'm suppose to place my api key?  client = OpenAI(  api\_key=os.environ['OPENAI\_API\_KEY'], # this is also the default, it can be omitted  ) |

All reactions

[![@earosenfeld](https://avatars.githubusercontent.com/u/28513443?s=60&v=4)](/earosenfeld)

#### [earosenfeld](/earosenfeld) [Nov 22, 2023](#discussioncomment-7646133)

|  |
| --- |
| is there a working example of this new code? |

All reactions

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 22, 2023](#discussioncomment-7647109) Author

|  |
| --- |
| Working examples demoing v1 are available here: <https://github.com/openai/openai-python/tree/main/examples> |

❤️
1

 
All reactions

* ❤️
  1

[![@skyl](https://avatars.githubusercontent.com/u/61438?s=60&v=4)](/skyl)

#### [skyl](/skyl) [Dec 16, 2023](#discussioncomment-7869396)

|  |
| --- |
| Putting api keys in environment variables is good practice compared to hardcoding them into your scripts. Maybe ask ChatGPT about it ;) |

👎
1

 
All reactions

* 👎
  1

[![@FlyingFathead](https://avatars.githubusercontent.com/u/86568670?s=60&v=4)](/FlyingFathead)

#### [FlyingFathead](/FlyingFathead) [Jan 3, 2024](#discussioncomment-8006597)

|  |
| --- |
| Just in the past days, I've been working on my Telegram bot code that uses the `openai` package version `1.6.1`. I noticed how much the API request has changed, here's a piece of my WIP code, and although disorganized, it's been working pretty well: <https://github.com/FlyingFathead/TelegramBot-OpenAI-API/blob/b5c315e8dc46109279659a3adf6a997791cdbe35/main.py#L411>  As you can see from the code, I had to more or less disassemble the payload and rebuild the request to use an `async` `httpx` client. Although I agree that the update caused a massive extra hassle, at least it's now been working without issues so far and with an up-to-date `openai` library. You can take a peek at the link to see if it's of any use in your particular implementation.  Like someone else commented, GPT-4 is not of much help with these API changes at the moment due to it not having been updated on any of this. However, you can i.e. try to have GPT-4 take a peek at the new implementation and/or A/B compare it with yours if it can come up with something that suits your needs. |

All reactions

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [RockChinQ](/RockChinQ) [Nov 10, 2023](#discussioncomment-7529054)

|  |
| --- |
| Hi there. While I use `openai.proxies`, I found that there's no `proxies` attr in openai module, but such code to set forward proxies is valid  ``` openai.proxies = {     'http': 'http://127.0.0.1:7890',     'https': 'http://127.0.0.1:7890', } ```  Should `proxies` attr be added to the module or add it to stub files to make it more intuitive? |

1
 You must be logged in to vote

All reactions

2 replies

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Nov 10, 2023](#discussioncomment-7529641) Author

|  |
| --- |
| Proxies will not be added to the module client; please instantiate a client for that: <https://github.com/openai/openai-python#configuring-the-http-client> |

All reactions

[![@Jerry-hyl](https://avatars.githubusercontent.com/u/86762065?s=60&v=4)](/Jerry-hyl)

#### [Jerry-hyl](/Jerry-hyl) [Dec 5, 2023](#discussioncomment-7762518)

|  |
| --- |
| Hello! Have you solve the problem? I still have no idea how to to set proxies for this new version of api, could you give me a demo? |

All reactions

43 hidden items

Load more…

### [endolith](/endolith) [Dec 21, 2023](#discussioncomment-7919744)

|  |
| --- |
| `openai migrate` breaks code because it doesn't make enough changes:  ``` TypeError: 'ChatCompletion' object is not subscriptable ```  You need to make additional changes by hand like:  ``` -output = completion['choices'][0]['message']['content'] -prom = completion['usage']['prompt_tokens'] -comp = completion['usage']['completion_tokens'] +output = completion.choices[0].message.content +prom = completion.usage.prompt_tokens +comp = completion.usage.completion_tokens ``` |

1
 You must be logged in to vote

All reactions

12 replies

Show 7 previous replies

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 10, 2024](#discussioncomment-8083840)

|  |
| --- |
| Yes, we can fix this. |

👍
1

 
All reactions

* 👍
  1

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 10, 2024](#discussioncomment-8085429)

|  |
| --- |
| [@endolith](https://github.com/endolith) Can you try the latest migration command? We added a [fix](https://github.com/getgrit/stdlib/pull/50) and test case based on your example. |

All reactions

[![@endolith](https://avatars.githubusercontent.com/u/58611?s=60&v=4)](/endolith)

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

#### [endolith](/endolith) [Jan 11, 2024](#discussioncomment-8090117)

|  |
| --- |
| I had already fixed it manually |

All reactions

[![@phongvants123](https://avatars.githubusercontent.com/u/64645010?s=60&v=4)](/phongvants123)

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

#### [phongvants123](/phongvants123) [Feb 10, 2024](#discussioncomment-8430193)

|  |
| --- |
| how did you fix it  [image](https://private-user-images.githubusercontent.com/64645010/303873560-a6c58746-cfff-4048-9c1d-c06a28ca58ee.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3ODk0MTU1MzEsIm5iZiI6MTc4OTQxNTIzMSwicGF0aCI6Ii82NDY0NTAxMC8zMDM4NzM1NjAtYTZjNTg3NDYtY2ZmZi00MDQ4LTljMWQtYzA2YTI4Y2E1OGVlLnBuZz9YLUFtei1BbGdvcml0aG09QVdTNC1ITUFDLVNIQTI1NiZYLUFtei1DcmVkZW50aWFsPUFLSUFWQ09EWUxTQTUzUFFLNFpBJTJGMjAyNjA5MTQlMkZ1cy1lYXN0LTElMkZzMyUyRmF3czRfcmVxdWVzdCZYLUFtei1EYXRlPTIwMjYwOTE0VDE5NDcxMVomWC1BbXotRXhwaXJlcz0zMDAmWC1BbXotU2lnbmF0dXJlPTMwYzhmY2E4ZWFiODU4Y2Y3NTlhMGFlMDc0NDM0YjJiNWJmMTk0OWEwMmE3NzUzMjkxOWM3MDhlYjA0MzM3NDUmWC1BbXotU2lnbmVkSGVhZGVycz1ob3N0JnJlc3BvbnNlLWNvbnRlbnQtdHlwZT1pbWFnZSUyRnBuZyJ9.frBPnHUCHv-gKVUHDuMxxNJBJbyzcFeEovtRlDPlr4Q) |

All reactions

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 10, 2024](#discussioncomment-8430199)

|  |
| --- |
| <https://platform.openai.com/docs/api-reference/chat> |

All reactions

### [MrPereir4](/MrPereir4) [Jan 6, 2024](#discussioncomment-8032929)

|  |
| --- |
| I'm encountering issues when trying to use the OpenAI API with AWS Lambda following an update that involves pydantic. It's reporting that there is no module named pydantic\_core. I am using Python 3.9 for both the Lambda function code and the OpenAI API layer.  **[ERROR] Runtime.ImportModuleError: Unable to import module 'lambda\_function': No module named 'pydantic\_core.\_pydantic\_core' Traceback (most recent call last):**  I'm receiving the error message above from the CloudWatch error log. |

2
 You must be logged in to vote

All reactions

2 replies

[![@andrehenriquemendes](https://avatars.githubusercontent.com/u/56480520?s=60&v=4)](/andrehenriquemendes)

#### [andrehenriquemendes](/andrehenriquemendes) [Jan 9, 2024](#discussioncomment-8065421)

|  |
| --- |
| Did you solve it? |

All reactions

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 10, 2024](#discussioncomment-8074899) Author

|  |
| --- |
| A separate GitHub issue would have been more appropriate for this. However, if any of the tips in [pydantic/pydantic#6557](https://github.com/pydantic/pydantic/issues/6557) fix your issue, you may not need to file one. |

All reactions

### [ResearcherSara](/ResearcherSara) [Jan 11, 2024](#discussioncomment-8089926)

|  |
| --- |
| Hello and thanks, I have a question and really thankful if you can help me out. I have installed openai(1.7.1), my python version is 3.11.7, in the first step of my code which is `import openai` in visual studio on windows I got "*dynamic module does not define module export function (PyInit\_\_pydantic\_core)*" **error**. I checked my pydantic(2.5.3) and pydantic\_core(2.15.0), they seem fine. This sounds a bit weird for me because I am unable to import openai in any circumstances. |

1
 You must be logged in to vote

All reactions

0 replies

### This comment was marked as off-topic.

[Sign in to view](/login?return_to=https%3A%2F%2Fgithub.com%2Fopenai%2Fopenai-python%2Fdiscussions%2F742)

### [EdricSouza](/EdricSouza) [Jan 15, 2024](#discussioncomment-8138046)

|  |
| --- |
| What can I do about 'OpenAIError'? because I need the exception:  ``` i = 0 embeddings = [] for text in df['text']:      time.sleep(2)      print(i)      try:          embedding = openai.Embedding.create(input=text, engine='text-embedding-ada-002')['data'][0]['embedding']          print("Embedding the text")          embeddings.append(embedding)      except OpenAIError as e:          if e.status == 429: # Check if the error is a Rate Limit              print("Rate limit error, waiting 20 seconds before trying again")              time.sleep(20)              # Retry the operation that caused the error          else:              # Handle other types of OpenAI errors here              raise # Reraise the error so it can be handled at another level      i+=1 ``` |

2
 You must be logged in to vote

All reactions

1 reply

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 16, 2024](#discussioncomment-8147224) Author

|  |
| --- |
| You're looking for `openai.APIError`, see <https://github.com/openai/openai-python?tab=readme-ov-file#handling-errors>  Note that the new library auto-retries automatically, so you may not need this code. |

👍
2

 
All reactions

* 👍
  2

### [Abs73](/Abs73) [Jan 16, 2024](#discussioncomment-8148056)

|  |
| --- |
| After following the guide for adding to Windows. I get the same result. what could be the issue? |

2
 You must be logged in to vote

All reactions

2 replies

[![@Abs73](https://avatars.githubusercontent.com/u/99238578?s=60&v=4)](/Abs73)

#### [Abs73](/Abs73) [Jan 17, 2024](#discussioncomment-8150782)

|  |
| --- |
| [image](https://private-user-images.githubusercontent.com/99238578/297228441-ba23b6f3-9619-42ac-8e47-a4ecf3d81e1c.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3ODk0MTU1MzEsIm5iZiI6MTc4OTQxNTIzMSwicGF0aCI6Ii85OTIzODU3OC8yOTcyMjg0NDEtYmEyM2I2ZjMtOTYxOS00MmFjLThlNDctYTRlY2YzZDgxZTFjLnBuZz9YLUFtei1BbGdvcml0aG09QVdTNC1ITUFDLVNIQTI1NiZYLUFtei1DcmVkZW50aWFsPUFLSUFWQ09EWUxTQTUzUFFLNFpBJTJGMjAyNjA5MTQlMkZ1cy1lYXN0LTElMkZzMyUyRmF3czRfcmVxdWVzdCZYLUFtei1EYXRlPTIwMjYwOTE0VDE5NDcxMVomWC1BbXotRXhwaXJlcz0zMDAmWC1BbXotU2lnbmF0dXJlPTE1ZmExODQ1ZjcyOTdmZWI0MGQ4MzgwMDViYjI2YmYzYmFmOTMxODM4ZDM0NGFjNTI0ZDhiYTFiYWMxNGU1ZDUmWC1BbXotU2lnbmVkSGVhZGVycz1ob3N0JnJlc3BvbnNlLWNvbnRlbnQtdHlwZT1pbWFnZSUyRnBuZyJ9.PaMpibBx5qIt8-qXtIhp4aJiifFpmyGqHAhtPyKnfVo) |

All reactions

[![@phongvants123](https://avatars.githubusercontent.com/u/64645010?s=60&v=4)](/phongvants123)

#### [phongvants123](/phongvants123) [Feb 10, 2024](#discussioncomment-8430226)

|  |
| --- |
| i got the same error, how did you solve it ?  [image](https://private-user-images.githubusercontent.com/64645010/303874344-466dc2ba-882f-4683-89e6-ca34b67e89eb.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3ODk0MTU1MzEsIm5iZiI6MTc4OTQxNTIzMSwicGF0aCI6Ii82NDY0NTAxMC8zMDM4NzQzNDQtNDY2ZGMyYmEtODgyZi00NjgzLTg5ZTYtY2EzNGI2N2U4OWViLnBuZz9YLUFtei1BbGdvcml0aG09QVdTNC1ITUFDLVNIQTI1NiZYLUFtei1DcmVkZW50aWFsPUFLSUFWQ09EWUxTQTUzUFFLNFpBJTJGMjAyNjA5MTQlMkZ1cy1lYXN0LTElMkZzMyUyRmF3czRfcmVxdWVzdCZYLUFtei1EYXRlPTIwMjYwOTE0VDE5NDcxMVomWC1BbXotRXhwaXJlcz0zMDAmWC1BbXotU2lnbmF0dXJlPTRkMjlkOWEyZTI3ZjI4MDVlZjBhOGIxNzRmNzI2YTQ1NjBmNjE5NDU0OTM2MjUxMzdkMmJlOTc4N2U4YzFlZTkmWC1BbXotU2lnbmVkSGVhZGVycz1ob3N0JnJlc3BvbnNlLWNvbnRlbnQtdHlwZT1pbWFnZSUyRnBuZyJ9.cGFC1xKxiPd7k1XhGRN5O5WxsZEVDL9r01wYrWS46QQ) |

All reactions

### [Madtolla](/Madtolla) [Jan 18, 2024](#discussioncomment-8167495)

|  |
| --- |
| Hello  I tried the solution for linux on raspberry Pi4 but It didn't worked `Traceback (most recent call last): File "/home/office/home/office/bin/openai", line 8, in <module> sys.exit(main()) ^^^^^^ File "/home/office/home/office/lib/python3.11/site-packages/openai/cli/_cli.py", line 129, in main _main() File "/home/office/home/office/lib/python3.11/site-packages/openai/cli/_cli.py", line 209, in _main parsed.func( File "/home/office/home/office/lib/python3.11/site-packages/openai/cli/_tools/migrate.py", line 56, in migrate subprocess.check_call([grit_path, "apply", "openai", *args.unknown_args]) File "/usr/lib/python3.11/subprocess.py", line 408, in check_call retcode = call(*popenargs, **kwargs) ^^^^^^^^^^^^^^^^^^^^^^^^^^ File "/usr/lib/python3.11/subprocess.py", line 389, in call with Popen(*popenargs, **kwargs) as p: ^^^^^^^^^^^^^^^^^^^^^^^^^^^ File "/usr/lib/python3.11/subprocess.py", line 1024, in __init__ self._execute_child(args, executable, preexec_fn, close_fds, File "/usr/lib/python3.11/subprocess.py", line 1901, in _execute_child raise child_exception_type(errno_num, err_msg, err_filename) FileNotFoundError: [Errno 2] No such file or directory: PosixPath('/home/office/.cache/openai-python/.install/bin/marzano')`  here is the error message  and what I did is just worte `openai migrate`  I need your help ASAP |

2
 You must be logged in to vote

All reactions

1 reply

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 24, 2024](#discussioncomment-8227438) Author

|  |
| --- |
| cc [@morgante](https://github.com/morgante) |

👍
1

 
All reactions

* 👍
  1

### [RomanG-PT](/RomanG-PT) [Jan 18, 2024](#discussioncomment-8171839)

|  |
| --- |
| how to enter in Ubuntu this 3 lines as it is accepts only one and than run it....  curl -fsSL <https://docs.grit.io/install> | bash  grit install  grit apply openai |

2
 You must be logged in to vote

All reactions

5 replies

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 18, 2024](#discussioncomment-8172641)

|  |
| --- |
| Can you share what the output is? |

All reactions

[![@RomanG-PT](https://avatars.githubusercontent.com/u/153621112?s=60&v=4)](/RomanG-PT)

#### [RomanG-PT](/RomanG-PT) [Jan 18, 2024](#discussioncomment-8173330)

|  |
| --- |
| here it is:  [image](https://private-user-images.githubusercontent.com/153621112/297856368-f282bf8c-38b3-45f8-9cae-d6292a91c588.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3ODk0MTU1MzEsIm5iZiI6MTc4OTQxNTIzMSwicGF0aCI6Ii8xNTM2MjExMTIvMjk3ODU2MzY4LWYyODJiZjhjLTM4YjMtNDVmOC05Y2FlLWQ2MjkyYTkxYzU4OC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYwOTE0JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MDkxNFQxOTQ3MTFaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT00YWRlYTljM2U1MWM1YTkzODIyOTgyZWI3OTc5YWI0Y2VkYzRhYzg1Y2JlMTg0NDgxNTcyMjU3NmYwMTZjNGE3JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.97xmHh4iopifUexTf1U_ZF4CUsrkam6lFAiVjVFy9DQ) |

All reactions

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 18, 2024](#discussioncomment-8173342)

|  |
| --- |
| Try sourcing it, run the last line of the install output. |

All reactions

[![@RomanG-PT](https://avatars.githubusercontent.com/u/153621112?s=60&v=4)](/RomanG-PT)

#### [RomanG-PT](/RomanG-PT) [Jan 18, 2024](#discussioncomment-8173406)

|  |
| --- |
| thank you for your help  I am so new to all this...  do you mean "run source /home/roman/.bashrc" ? |

All reactions

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 18, 2024](#discussioncomment-8173511)

|  |
| --- |
| Yes, run this command:  ``` source /home/roman/.bashrc ``` |

All reactions

### [RomanG-PT](/RomanG-PT) [Jan 18, 2024](#discussioncomment-8173619)

|  |
| --- |
| maybe I am doing something very stupid, but nothing happens:  [image](https://private-user-images.githubusercontent.com/153621112/297865544-b39ce9d7-f2bd-46a8-9e70-b280cac5031b.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3ODk0MTU1MzEsIm5iZiI6MTc4OTQxNTIzMSwicGF0aCI6Ii8xNTM2MjExMTIvMjk3ODY1NTQ0LWIzOWNlOWQ3LWYyYmQtNDZhOC05ZTcwLWIyODBjYWM1MDMxYi5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYwOTE0JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MDkxNFQxOTQ3MTFaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT0wZjMwMDI1MjFlZmY1YmU2OTI5YjBjYzkzODgwZTJmMWMwZTBmNTcyZGZjMDA0MGQ0ZTFmOGMwMjVkYzY3ZjFmJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.8PLc5SIGWVIbN5CRv1Eno3nldYefxFnEmAJvE9KfIbw) |

1
 You must be logged in to vote

All reactions

1 reply

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Jan 18, 2024](#discussioncomment-8173628)

|  |
| --- |
| It's expected for nothing you happen from that. You then need to actually run the migration.  If you join our Discord I'm happy to help you - <https://discord.com/invite/ARExD4gvFB> |

All reactions

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [EdricSouza](/EdricSouza) [Jan 20, 2024](#discussioncomment-8192441)

|  |
| --- |
| Can anyone help me with this code? I'm trying to run this function, but this error appears:  (At first the idea was to use openai.embeddings\_utils, but with the openai update, I don't know what to do)  File "c:\Users", line 254, in distances\_from\_embeddings  distances = np.dot(embeddings, query\_embedding) / (np.linalg.norm(embeddings) \* np.linalg.norm(query\_embedding))  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ValueError: shapes (9,) and (1,1536) not aligned: 9 (dim 0) != 1 (dim 0)  ``` def distances_from_embeddings(query_embedding, embeddings, distance_metric='cosine'):     if distance_metric == 'cosine':         query_embedding = np.expand_dims(query_embedding, 0)         print("Forma original de embeddings:", embeddings.shape)         print("Forma original de query_embedding:", query_embedding.shape)         distances = np.dot(embeddings, query_embedding) / (np.linalg.norm(embeddings) * np.linalg.norm(query_embedding))         distances = 1 - distances       else:         raise ValueError("Métrica de distância não suportada. Utilize 'cosine'.")      return distances ```  ``` def create_context(question, df, max_len=1800, size="ada"):      # Obter a embeddings para a pergunta que foi feita     q_embeddings = client.embeddings.create(input=question,model='text-embedding-ada-002').data[0].embedding      # Obter as distâncias a partir dos embeddings     df['distances'] = distances_from_embeddings(q_embeddings, df['embeddings'].values, distance_metric='cosine')       returns = []     cur_len = 0      # Classifique por distância e adicione o texto ao contexto     for i, row in df.sort_values('distances', ascending=True).iterrows():                  # Adicionar o comprimento do texto ao comprimento atual         cur_len += row['n_tokens'] + 4                  # Se o contexto for muito longo, quebre         if cur_len > max_len:             break                  # Caso contrário, adicione-o ao texto que está sendo retornado         returns.append(row["text"])      # Retornar o contexto     return "\n\n###\n\n".join(returns) ``` |

1
 You must be logged in to vote

😕
1

 
All reactions

* 😕
  1

1 reply

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Jan 24, 2024](#discussioncomment-8227440) Author

|  |
| --- |
| For help developing with the OpenAI API from fellow developers, I recommend the [OpenAI Discord](https://discord.com/invite/openai) server! |

All reactions

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [Stratosss](/Stratosss) [Feb 1, 2024](#discussioncomment-8339041)

|  |
| --- |
| Hello, I am a little bit confused with the steps:  "In the WSL terminal, cd into the appropriate directory (e.g., cd /mnt/c/Users/Myself/my/code/) and then run the following commands:  curl -fsSL <https://docs.grit.io/install> | bash  grit install  grit apply openai"   1. What do you mean by "appopriate directory"? Is it a random directory i have to cd in? 2. I selected a random directory, run the first command which was executed fine and returned: "`grit was installed successfully to ~/.grit/bin/grit"` 3. When I try to perform "grit install" no matter the directory I'm in it says:   ``` Command 'grit' not found, did you mean:   command 'grip' from deb grip (4.2.0-3)   command 'grig' from deb grig (0.8.1-6)   command 'grim' from deb grim (1.4.0+ds-1)   command 'crit' from deb criu (3.16.1-2)   command 'git' from deb git (1:2.34.1-1ubuntu1.10) ```  I tried the above while being in the randomly chosen directory and in ~/.grit/bin/grit. Nothing works. Please help!  Running it on Windows 11 Version 10.0.22621 Build 22621  and have WSL already installed  thanks! |

1
 You must be logged in to vote

All reactions

1 reply

[![@morgante](https://avatars.githubusercontent.com/u/1627801?s=60&v=4)](/morgante)

#### [morgante](/morgante) [Feb 1, 2024](#discussioncomment-8339174)

|  |
| --- |
| You need to CD into the directory where your OpenAI code is.  After installing Grit, you need to source it - or you can just reference it directly by path. Try this, from your OpenAI dir:  ``` ~/.grit/bin/grit apply openai ```  If you require further assistance, please join our Discord: <https://docs.grit.io/discord> |

👍
1

 
All reactions

* 👍
  1

### [MeranTumeh](/MeranTumeh) [Feb 4, 2024](#discussioncomment-8360567)

|  |
| --- |
| Hi [@rattrayalex](https://github.com/rattrayalex),  I'm facing an issue with (openai.Deployment) since it was removed  I'm using Azure OpenAI, what is the alternative for it? |

1
 You must be logged in to vote

All reactions

3 replies

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Feb 4, 2024](#discussioncomment-8362352) Author

|  |
| --- |
| I believe that using Azure's SDKs is best for managing Azure resources like cognitive services deployments!  cc [@kristapratico](https://github.com/kristapratico) - is that right? |

All reactions

[![@kristapratico](https://avatars.githubusercontent.com/u/31998003?s=60&v=4)](/kristapratico)

#### [kristapratico](/kristapratico) [Feb 5, 2024](#discussioncomment-8373052)

|  |
| --- |
| That's correct. Deployments APIs were deprecated in the v0.X version of the openai library and so they were not added for v1.X. For all code managing deployments, please use the [azure-mgmt-cognitiveservices](https://pypi.org/project/azure-mgmt-cognitiveservices) client library. This library has additional options for working with deployments, like setting a deployment name or specific model version, and is the place where new features for deployments will be added.  Here are some samples for interacting with deployments:  [Create a deployment](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/put_deployment.py)  [Retrieve a deployment](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/get_deployment.py)  [List deployments](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/list_deployments.py)  [Delete a deployment](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/delete_deployment.py) |

😕
1

 
All reactions

* 😕
  1

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Feb 5, 2024](#discussioncomment-8374156) Author

|  |
| --- |
| Thanks Krista! I've [updated](https://arc.net/l/quote/eqblnddq) the migration guide above to specify this. |

❤️
1

 
All reactions

* ❤️
  1

### [PierrunoYT](/PierrunoYT) [Feb 6, 2024](#discussioncomment-8387205)

|  |
| --- |
| The Docs on the Open AI Website is outdated |

3
 You must be logged in to vote

All reactions

3 replies

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=60&v=4)](/rattrayalex)

#### [rattrayalex](/rattrayalex) [Feb 6, 2024](#discussioncomment-8387970) Author

|  |
| --- |
| Can you provide a link to the part(s) of the docs which need updating? |

All reactions

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 7, 2024](#discussioncomment-8397576)

|  |
| --- |
| This is the only doc which works <https://platform.openai.com/docs/api-reference/images> and Chat GPT is messing up things with it |

All reactions

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 7, 2024](#discussioncomment-8397583)

|  |
| --- |
| This does not work <https://platform.openai.com/docs/guides/images/usage?context=node> |

All reactions

edited

### Uh oh!

There was an error while loading. Please reload this page.

{{actor}} deleted this content
.

Something went wrong.

### Uh oh!

There was an error while loading. Please reload this page.

### [aiakubovich](/aiakubovich) [Feb 7, 2024](#discussioncomment-8393320)

|  |
| --- |
| terrible update. mess up all scripts. APIs are not consistent e.g. base\_url for OpenAI() and azure\_endpoint for AzureOpenAI() |

7
 You must be logged in to vote

All reactions

1 reply

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 10, 2024](#discussioncomment-8430204)

|  |
| --- |
| <https://platform.openai.com/docs/api-reference/chat> |

All reactions

### [411A](/411A) [Feb 10, 2024](#discussioncomment-8428530)

|  |
| --- |
| Thanks to migration from `aiohttp` to `httpx` and not providing any methods or proper documentations, your update broke my `aiohttp` optimization functions (`openai.aiosession.set(ClientSession()`) that all my openai functions depend on. Will stay on previous version without TTS. **ZERO backward compatibility**. |

2
 You must be logged in to vote

All reactions

1 reply

[![@PierrunoYT](https://avatars.githubusercontent.com/u/95778421?s=60&v=4)](/PierrunoYT)

#### [PierrunoYT](/PierrunoYT) [Feb 10, 2024](#discussioncomment-8430203)

|  |
| --- |
| <https://platform.openai.com/docs/api-reference/chat> |

All reactions

Category

[💬

General](/openai/openai-python/discussions/categories/general)

Labels

None yet

94 participants

[![@rattrayalex](https://avatars.githubusercontent.com/u/704302?s=48&v=4)](/rattrayalex) [![@abi](https://avatars.githubusercontent.com/u/23818?s=48&v=4)](/abi) [![@endolith](https://avatars.githubusercontent.com/u/58611?s=48&v=4)](/endolith) [![@skyl](https://avatars.githubusercontent.com/u/61438?s=48&v=4)](/skyl) [![@gojira](https://avatars.githubusercontent.com/u/137710?s=48&v=4)](/gojira) [![@antont](https://avatars.githubusercontent.com/u/201016?s=48&v=4)](/antont) [![@zestyping](https://avatars.githubusercontent.com/u/236086?s=48&v=4)](/zestyping) [![@ppravdin](https://avatars.githubusercontent.com/u/322894?s=48&v=4)](/ppravdin) [![@athyuttamre](https://avatars.githubusercontent.com/u/1485350?s=48&v=4)](/athyuttamre) [![@morgante](https://avatars.githubusercontent.com/u/1627801?s=48&v=4)](/morgante) [![@giopoi](https://avatars.githubusercontent.com/u/1670210?s=48&v=4)](/giopoi) [![@kenhuangus](https://avatars.githubusercontent.com/u/1869785?s=48&v=4)](/kenhuangus) [![@urbanscribe](https://avatars.githubusercontent.com/u/2935311?s=48&v=4)](/urbanscribe) [![@hatgit](https://avatars.githubusercontent.com/u/5213035?s=48&v=4)](/hatgit) [![@roland71](https://avatars.githubusercontent.com/u/6247515?s=48&v=4)](/roland71) [![@vnktsh](https://avatars.githubusercontent.com/u/6447905?s=48&v=4)](/vnktsh) [![@evemilano](https://avatars.githubusercontent.com/u/7690274?s=48&v=4)](/evemilano) [![@s2terminal](https://avatars.githubusercontent.com/u/7953751?s=48&v=4)](/s2terminal) [![@arran549](https://avatars.githubusercontent.com/u/8976812?s=48&v=4)](/arran549) [![@richlysakowski](https://avatars.githubusercontent.com/u/9345711?s=48&v=4)](/richlysakowski) [![@LukasNel](https://avatars.githubusercontent.com/u/11474572?s=48&v=4)](/LukasNel) and others

Heading

Bold

Italic

Quote

Code

Link

---

Numbered list

Unordered list

Task list

---

Attach files

Mention

Reference

 

Menu

* Heading
* Bold
* Italic
* Quote
* Code
* Link
* Numbered list
* Unordered list
* Task list
* Attach files
* Mention
* Reference

 

# Select a reply

Loading

### Uh oh!

There was an error while loading. Please reload this page.

[Create a new saved reply](/settings/replies?return_to=1)

 👍
1
  reacted with thumbs up emoji

 👎
1
  reacted with thumbs down emoji

 😄
1
  reacted with laugh emoji

 🎉
1
  reacted with hooray emoji

 😕
1
  reacted with confused emoji

 ❤️
1
  reacted with heart emoji

 🚀
1
  reacted with rocket emoji

 👀
1
  reacted with eyes emoji