"""Run by scripts/setup_job_env.sh autogluon, in that environment, after installing it.

AutoGluon multimodal downloads NLTK data (for text augmentation) the first
time it trains or predicts, from every data-loader process at once; the
downloads collide and each prediction then retries them. Fetching them here
once avoids that. They go to NLTK_DATA if set (the container images set it,
so the non-root user can read them), else ~/nltk_data.
"""

import os

import nltk

for name in ("averaged_perceptron_tagger_eng", "averaged_perceptron_tagger", "wordnet", "omw-1.4"):
    nltk.download(name, quiet=True, download_dir=os.environ.get("NLTK_DATA"))
print("NLTK data ready")
