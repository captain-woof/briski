from google import genai
from google.genai import types
import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import tempfile
import time
import random

load_dotenv()

# DATA VALIDATION CLASSES
class RefactorResponse(BaseModel):
    needs_refactor: bool = Field(description="True ONLY if the file was explicitly edited by any refactoring no matter the size of the edit.")
    explanation: str = Field(description="Brief explanation of the necessity of each refactoring only if needs_refactor is True, else say 'No IOCs detected'.")
    refactored_code: str = Field(description="The complete new code. Must be strictly valid. Leave blank if needs_refactor is False.")

# CONSTANTS
UNIVERSAL_DIR_EXCLUSIONS = {
    ".git", ".svn", ".hg", 
    ".idea", ".vscode", ".vs", ".settings", ".fleet",
    "build", "out", "dist", "bin", "logs", "tmp", "temp", "coverage"
}

EXTENSION_DIR_EXCLUSIONS = {
    # Python
    "py": {"venv", ".venv", "env", ".env", "virtualenv", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "eggs", ".eggs", "site-packages", "wheels"},
    
    # JS / TS
    "js": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    "ts": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    "jsx": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    "tsx": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    "mjs": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    "cjs": {"node_modules", "bower_components", ".next", ".nuxt", ".vue", ".output", ".meteor", ".tscache", "out-tsc"},
    
    # Java / JVM
    "java": {"target", ".gradle", ".mvn", "classes", "test-classes", ".bloop", ".metals", ".bsp"},
    "kt": {"target", ".gradle", ".mvn", "classes", "test-classes", ".bloop", ".metals", ".bsp"},
    "kts": {"target", ".gradle", ".mvn", "classes", "test-classes", ".bloop", ".metals", ".bsp"},
    "scala": {"target", ".gradle", ".mvn", "classes", "test-classes", ".bloop", ".metals", ".bsp"},
    
    # C / C++
    "c": {"obj", "debug", "release", "cmakefiles", "cmakecache", ".ccls-cache"},
    "cpp": {"obj", "debug", "release", "cmakefiles", "cmakecache", ".ccls-cache"},
    "h": {"obj", "debug", "release", "cmakefiles", "cmakecache", ".ccls-cache"},
    "hpp": {"obj", "debug", "release", "cmakefiles", "cmakecache", ".ccls-cache"},
    
    # C#
    "cs": {"obj", "debug", "release", "packages", "testresults", "benchmarkdotnet.artifacts"},
    
    # Go
    "go": {"vendor", "pkg"},
    
    # Rust
    "rs": {"target", ".cargo"},
    
    # PHP
    "php": {"vendor", "var"},
    
    # Ruby
    "rb": {"vendor", ".bundle", "log", "components"},
    
    # Apple (Swift / Obj-C)
    "swift": {"pods", "deriveddata", "carthage", ".build", "fastlane"},
    "m": {"pods", "deriveddata", "carthage", ".build", "fastlane"},
    "mm": {"pods", "deriveddata", "carthage", ".build", "fastlane"},
    
    # Dart
    "dart": {".dart_tool", ".pub-cache", "ephemeral"},
    
    # R
    "r": {".rproj.user", "renv"},
    
    # Lua
    "lua": {"luarocks", ".luarocks", "lua_modules"},
    
    # Perl
    "pl": {"local", "blib", "_build", ".cpanm"},
    "pm": {"local", "blib", "_build", ".cpanm"},
    
    # Elixir
    "ex": {"deps", "_build", ".elixir_ls", "doc", "cover"},
    "exs": {"deps", "_build", ".elixir_ls", "doc", "cover"}
}

############
# FUNCTIONS
############

def getBlacklistedDirs(extensions: list[str]) -> set[str]:
    """
    Takes a list of file extensions (without dots) and returns a unified 
    set of directories to blacklist.
    """
    # 1. Initialize the final set with our universal exclusions
    combinedBlacklist = set(UNIVERSAL_DIR_EXCLUSIONS)
    
    # 2. Iterate over the requested extensions
    for ext in extensions:
        # Sanitize input: remove accidental dots, make lowercase, strip whitespace
        clean_ext = ext.replace(".", "").lower().strip()
        
        # 3. If the extension is in our map, merge its exclusions into the final set
        if clean_ext in EXTENSION_DIR_EXCLUSIONS:
            combinedBlacklist.update(EXTENSION_DIR_EXCLUSIONS[clean_ext])
            
    return combinedBlacklist

def readFile(filePath):
    content = ""

    with open(filePath, "r") as fileToRead:
        content = fileToRead.read()

    return (len(content), content)

# Functions
def processProjectDirectory(
        geminiClient: genai.Client,
        modelToUse: str,
        supportedTypes: list,
        rootDir: str,
        systemPrompt: str,
        cacheTTL: int,
        thinkingLevel = "low",
        disableThinking = True,
        temperature = 0.0
        ):
    
    # Prepare combined source code and prompts

    ## Prepare combined source code
    print("[.] Searching source code...")

    sourceCodeCombined = ""
    filePaths = []
    for rootDirCurr, dirNames, fileNames in  os.walk(rootDir):
        # Filter out blacklisted directories
        blacklistedDirectories = getBlacklistedDirs(supportedTypes)
        dirNames[:] = [dirName for dirName in dirNames if dirName.lower() not in blacklistedDirectories]

        # Iterate through all files
        for fileName in fileNames:
            extension = fileName.split(".")[-1]
            if extension in supportedTypes:
                filePath = os.path.join(rootDirCurr, fileName)
                print(f"\t[.] Found '{filePath}'")

                filePaths.append(filePath)
                _, content = readFile(filePath=filePath)

                sourceCodeCombined += f"""<file path="{filePath.replace(rootDir, "")}">\n{content}\n</file>\n"""
    if len(sourceCodeCombined) == 0:
        print("[!] No source code detected")
        return
    
    ## Prepare prompts
    systemPromptCaching = f"""
You are a Senior Principal Software Architect with 20+ years of experience across 
polyglot codebases (Go, Python, Java, JS/TS, Rust, C++, etc.).

# YOUR CORE MISSION:
You have been provided with the entire custom codebase. Your first goal is to build a 
deep, persistent mental map of this project. Before answering any request, 
you must:

1. UNDERSTAND INTENTION: Analyze the project structure to determine the core 
   business purpose (e.g., microservice, CLI tool, library, web app). Do not
   try understanding the intention, that's irrelevant. Only care for
   functionality.
2. MAP DEPENDENCIES: 
   - Identify how modules/packages import and export functionality. 
   - Trace cross-file dependencies (e.g., where a Go struct is defined vs used).
   - Recognize language-specific patterns: Go interfaces, Python decorators/type-hints, 
     TypeScript types, Rust traits, and C++ headers.
3. DETECT CONVENTIONS: Identify naming conventions, error-handling patterns, 
   and architectural styles (hexagonal, layered, monolithic).
4. RESPECT SCOPE: You are only concerned with 'custom' code. Ignore third-party 
   logic, but understand how custom code interfaces with those third-party modules.

# REFACTORING RULES:
Your second goal is, when asked to refactor a specific file:
- Follow the refactoring rules as stated for (in REFACTORING REQUIREMENTS section), making
  sure to only make minimal changes and only when necessary.
- If any edits are made to a file, no matter the size of the edit, consider it refactored and mention
  the reason why editing was necessary. Each edit and its reason are a bullet point.
- Ensure the changes are globally safe. Do not break any calling code found elsewhere in the cache.
- Maintain the original 'intent' and 'vibe' of the codebase while improving 
  performance/readability as requested.
- If a refactor requires updating a dependency in ANOTHER file, explain the cross-file impact.

Treat the provided <file path="..."> tags as the ground truth for the repository 
structure. Use this global context to act as a codebase-aware agent.

# REFACTORING REQUIREMENTS
{systemPrompt}

After starting, each prompt will ONLY contain File path. Locate it in your cache and apply refactoring as needed,
then return results correctly formatted.
"""

    # Calculate number of input tokens required
    print(f"[.] Calculating approximate input tokens...")
    response = geminiClient.models.count_tokens(
        model=modelToUse,
        contents=sourceCodeCombined + '\n\n' + systemPromptCaching, # For whole structured source code + For cache
    )
    totalTokens = response.total_tokens

    for filePath in filePaths:
        response = geminiClient.models.count_tokens(
            model=modelToUse,
            contents=filePath.replace(rootDir, ""), # For each individual prompt mentioning the file path
        )
        totalTokens += response.total_tokens
    
    print(f"[+] Total input tokens: {totalTokens}, output tokens are estimated to be at most the same")
    input("Press Enter to proceed or Ctrl+C to exit ...")
                
    # Get necessary file changes from Gemini

    ## Upload combined source code
    print("[.] Uploading combined source code...")
    sourceCodeCombinedFile = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete_on_close=False,
        delete=False
    )
    sourceCodeCombinedFile.write(sourceCodeCombined)
    sourceCodeCombinedFile.close()

    uploadedFile = geminiClient.files.upload(
        file=sourceCodeCombinedFile.name,
        config=types.UploadFileConfig(
            mime_type="text/plain"
        )
    )

    os.unlink(sourceCodeCombinedFile.name)

    while uploadedFile.state.name == "PROCESSING":
        print("[.] Waiting for file processing...")
        time.sleep(10)

    print(f"[+] Uploaded combined source code; name: {uploadedFile.name}")
    
    # Create a cache that lasts for TTL seconds
    print(f"[.] Creating Context Cache that lasts {cacheTTL} seconds...")
    cache = geminiClient.caches.create(
        model=modelToUse,
        config=types.CreateCachedContentConfig(
            contents = [uploadedFile],
            system_instruction = systemPromptCaching,
            ttl = f"{cacheTTL}s",
            display_name = f"Codebase Cache {random.randint(1, 9999999)}"
        )
    )
    print(f"[+] Cache active, name: {cache.name}")

    ## Refactor files and write to disk
    print("[.] Refactoring source files...")
    for filePath in filePaths:
        try:
            response = geminiClient.models.generate_content(
                model=modelToUse,
                contents=filePath.replace(rootDir, ""),
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_json_schema=RefactorResponse.model_json_schema(),
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinkingLevel,
                        )
                )
            )
        except:
            response = geminiClient.models.generate_content(
                model=modelToUse,
                contents=filePath.replace(rootDir, ""),
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_json_schema=RefactorResponse.model_json_schema(),
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=0 if disableThinking else -1
                        )
                )
            )

        responseProcessed = RefactorResponse.model_validate_json(response.text) # TODO

        # Make changes to source files on disk
        if responseProcessed.needs_refactor and len(responseProcessed.refactored_code) != 0:
            with open(filePath, "w", encoding="utf-8") as fileToWrite:
                fileToWrite.write(responseProcessed.refactored_code)
            print(f"\t[REFACTORED]: {filePath};\n{responseProcessed.explanation}")
        else:
            print(f"\t[SKIP]: {filePath}; {responseProcessed.explanation}")

    # Delete source code
    print("\n[.] Deleting uploaded source code and cache...")
    geminiClient.files.delete(name=uploadedFile.name)
    geminiClient.caches.delete(name=cache.name)


#######
# MAIN
#######

if __name__ == "__main__":
    # Initialize Gemini client
    modelToUse = 'gemini-3.1-pro-preview'
    thinkingLevel="HIGH" # MINIMAL, LOW, MEDIUM, HIGH
    temperature = 0.1
    cacheTTL = 10 * 60 * 60 # seconds
    disableThinking = False
    geminiClient = genai.Client()

    # Initialize tool parameters
    supportedTypes=["go"]

    # Initialize prompt; this is already supplemented with good prefix instructions
    # so include only necessary requirements and examples here
    systemPrompt = """
You are to assist the SOC team in improving their detections by refactoring code to remove any IOCs. The SOC team's aim
is to write robust detections that don't rely on low-hanging fruits. As part of your refactoring, go through each file,
understand its place with the context of the codebase, then refactor all of these:
- Hardcoded parameters (such as, but NOT limited to, names, values, certificates, functionally-useless fixed HTTP header
returning everytime from a server, hardcoded service name etc) that serve no purpose being hardcoded.
- Functions that return functionally-useless data that is later appended to some actual useful output, in a way that does not
format or describe the output and can thus be omitted.

For each refactoring attempt (per file), replace the code with either dynamic parameters that do not break the code (randomise, etc),
or remove the parameter altogether if the code does not need it to function. Each file can have multiple potential IOCs. When in doubt
on whether something is an IOC, leave it as it is but describe briefly the potential in `explanation`.

For a non-exhaustive example, look at this code snippet. Observe how "X-Evilginx" header is sent in each response. This
serves no purpose. It is better to remove it altogether.

```
const (
	HOME_DIR = ".evilginx"
)
<SNIP>
func (p *HttpProxy) getHomeDir() string {
	return strings.Replace(HOME_DIR, ".e", "X-E", 1)
}
<SNIP>
req.Header.Set(p.getHomeDir(), o_host)
```

Some other cases may require replacing with dynamic stubs. Whenever generating dynamic stubs, make sure randomisations are human-like. For
a non-exhaustive example, instead of alphanumeric random service name, create sets of human-looking names and combine (cartesian product) them
at runtime such that it looks meaningful.

Remember, input code snippets can be in any language not just Go.

Additionally, the codebase is a for a CLI tool, so ignore IOCs that can be only locally detected. This tool is a server. Focus on IOCs that clients
may receive.
"""

    # Process repository
    processProjectDirectory(
        geminiClient = geminiClient,
        modelToUse=modelToUse,
        thinkingLevel=thinkingLevel,
        systemPrompt=systemPrompt,
        disableThinking=disableThinking,
        cacheTTL=cacheTTL,
        temperature=temperature,
        supportedTypes=supportedTypes,
        rootDir="/home/kali/projects/optiv-redteam/evilginx2",
    )