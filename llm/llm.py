from langchain_google_genai import GoogleGenerativeAIEmbeddings
from config import Config
from langchain_openai import ChatOpenAI


def get_gemini():
    api_key = Config.OPENROUTER_API_KEY

    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY must be set."
        )

    return ChatOpenAI(
        model=Config.OPENAI_MODEL,  # لازم تبقى "openai/gpt-5.6-luna"
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        extra_body={"reasoning": {"effort": "medium"}},
        temperature=0.0,
    )


def get_gemini_embeddings():
    api_key = Config.GEMINI_API_KEY

    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY or GOOGLE_API_KEY must be set."
        )

    return GoogleGenerativeAIEmbeddings(
        model=Config.EMBEDDING_MODEL,
        google_api_key=api_key,
        output_dimensionality=768,
    )