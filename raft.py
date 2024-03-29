from typing import Literal, Any
import argparse
from openai import OpenAI
from datasets import Dataset, load_dataset
from transformers import AutoTokenizer
import json
import os
import random
from langchain_experimental.text_splitter import SemanticChunker
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain_community.embeddings import HuggingFaceEmbeddings
from transformers.utils import logging
import argparse
from pdfminer.high_level import extract_text
from sentence_transformers import SentenceTransformer, CrossEncoder, util
from transformers import AutoTokenizer, AutoModelForCausalLM, TextStreamer
from torch import cuda, bfloat16
from torch import cuda
import transformers
from langchain.vectorstores import faiss
import os
import json
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
import nltk
from tqdm import tqdm
from datasets import DatasetDict, Dataset
import pickle
import re
import torch
import numpy as np
import pandas as pd
from colored import fg, bg, attr
from transformers import Pipeline, Conversation
from langchain.chains import ConversationChain
import faiss as fa
import re
logging.set_verbosity(40)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['CURL_CA_BUNDLE'] = ''


DocType = Literal["api", "pdf", "json", "txt"]
model_id = "mistralai/Mistral-7B-Instruct-v0.1"
device = f'cuda:{cuda.current_device()}' if cuda.is_available() else 'cpu'
custom_cache_dir = "../../../../Hard_Disk-2/cache_coewdl"

bnb_config = transformers.BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type='nf4',
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=bfloat16
)

model_chat = AutoModelForCausalLM.from_pretrained(model_id,
                                    trust_remote_code=True,
                                    quantization_config=bnb_config,
                                    device_map=device,
                                    cache_dir=custom_cache_dir)
model_chat.eval()

tokenizer = transformers.AutoTokenizer.from_pretrained(model_id,)

def get_args() -> argparse.Namespace:
    """
    Parses and returns the arguments specified by the user's command
    """
    parser = argparse.ArgumentParser()

    parser.add_argument("--datapath", type=str, default="", help="The path at which the document is located")
    parser.add_argument("--output", type=str, default="./", help="The path at which to save the dataset")
    parser.add_argument("--distractors", type=int, default=3, help="The number of distractor documents to include per data point / triplet")
    parser.add_argument("--p", type=float, default=1.0, help="The percentage that the oracle document is included in the context")
    parser.add_argument("--questions", type=int, default=5, help="The number of data points / triplets to generate per chunk")
    parser.add_argument("--chunk_size", type=int, default=512, help="The size of each chunk in number of tokens")
    parser.add_argument("--doctype", type=str, default="pdf", help="The type of the document, must be one of the accepted doctypes", choices=["pdf", "txt", "json", "api"])
    parser.add_argument("--openai_key", type=str, default="", help="Your OpenAI key used to make queries to GPT-3.5 or GPT-4")

    args = parser.parse_args()
    return args


def get_chunks(
    file_path: str, 
    doctype: DocType = "pdf", 
    chunk_size: int = 512, 
    openai_key: str = None
) -> list[str]:
    """
    Takes in a `file_path` and `doctype`, retrieves the document, breaks it down into chunks of size
    `chunk_size`, and returns the chunks.
    """
    chunks = []
    
    if doctype == "api":
        with open(file_path) as f:
            api_docs_json = json.load(f)
        chunks = list(api_docs_json)
        chunks = [str(api_doc_json) for api_doc_json in api_docs_json]

        for field in ["user_name", "api_name", "api_call", "api_version", "api_arguments", "functionality"]:
            if field not in chunks[0]:
                raise TypeError(f"API documentation is not in the format specified by the Gorilla API Store: Missing field `{field}`")

    else:
        if doctype == "json":
            with open(file_path, 'r') as f:
                data = json.load(f)
            text = data["text"]
        elif doctype == "pdf":
            text = ""
            with open(file_path, 'rb') as file:
                reader = PyPDF2.PdfReader(file)
                num_pages = len(reader.pages)
                for page_num in range(num_pages):
                    page = reader.pages[page_num]
                    text += page.extract_text()
        elif doctype == "txt":
            with open(file_path, 'r') as file:
                data = file.read()
            text = str(data)
        else:
            raise TypeError("Document is not one of the accepted types: api, pdf, json, txt")
        custom_model_directory = "/Hard_Disk-2/cache_coewdl/"
   
        model_dir_of_smart_chunking = "sentence-trans/finetuned-all-mpnet-base-v2"

        model_for_smart_chunking = HuggingFaceEmbeddings(model_name=custom_model_directory + model_dir_of_smart_chunking, show_progress = True)
        num_chunks = len(text) / chunk_size 
        # text_splitter = SemanticChunker(OpenAIEmbeddings(openai_api_key=OPENAPI_API_KEY), number_of_chunks=num_chunks)
        text_splitter = SemanticChunker(model_for_smart_chunking, number_of_chunks=num_chunks, breakpoint_threshold_type="percentile", breakpoint_threshold_amount=75)
        chunks = text_splitter.create_documents([text])
        chunks = [chunk.page_content for chunk in chunks]
            
    return chunks

def generate_instructions(api_call: Any, x=5) -> list[str]:
    """
    Generates `x` questions / use cases for `api_call`. Used when the input document is of type `api`.
    """
    response = client.chat.completions.create(
        model="gpt-3.5",
        messages=[
            {"role": "system", "content": "You are a synthetic instruction-api pair generator. Given an API endpoint in the form of a JSON object, generate %s example queries of instructions a user could ask and would be answered by invoking the API call. For example, if the given API call is the `service.users().getProfile(userId='me').execute()` call from the Gmail API, an example query could be 'How can I fetch my Gmail account's email address?'" % (x)},
            {"role": "system", "content": "The API endpoint is a JSON object with required params: user_name, api_name, api_call, api_version, api_arguments, functionality, and optional params: env_requirements, example_code, meta_data, Questions"},
            {"role": "system", "content": "For instance, if the api call contains: {'user_name': 'felixzhu555', 'api_name': 'Google Maps - Address Validation', 'api_call': 'Client.addressvalidation(addressLines, regionCode=region_code, locality=locality, enableUspsCass=boolean)', 'api_version': '4.10.0', 'api_arguments': {}, 'functionality': 'Validate an address and its components, standardize the address for mailing, and determine the best known geocode for it.', 'env_requirements': ['googlemaps'], 'example_code': 'client = googlemaps.Client(key='YOUR_API_KEY')\nresponse = client.addressvalidation('1600 Amphitheatre Pk', regionCode='US', locality='Mountain View', enableUspsCass=True)', 'meta_data': {'description': 'The googlemaps python client is an abstraction for the Google Maps API that requires python 3.5+. Each Google Maps web service request requires an API key or client ID. API keys are generated in the 'Credentials' page of the 'APIs & Services' tab of Google Cloud console. This key should be kept secret on your server.'}, 'questions': []}, an example instruction would be 'Validate the following address: University Avenue and, Oxford St, Berkeley, CA 94720.'"},
            {"role": "system", "content": "Don't mention 'API' or use any hints or the name of the API. In one-third of the queries, make sure to include a specific example, like 'Validate this address: 123 Harrison St, Oakland CA'. Include ONLY the queries in your response."},
            {"role": "user", "content": str(api_call)}
        ]
    )

    queries = response.choices[0].message.content.split('\n')
    queries = [strip_str(q) for q in queries]
    queries = [q for q in queries if any(c.isalpha() for c in q)]

    
    return queries

def generate_instructions_gen(chunk: Any, x: int = 5) -> list[str]:
    """
    Generates `x` questions / use cases for `chunk`. Used when the input document is of general types 
    `pdf`, `json`, or `txt`.
    """

    """Same with Hugging face local model"""

    messages = [
        # {"role": "system", "content": "You are a synthetic question-answer pair generator. Given a chunk of context about some topic(s), generate %s example questions a user could ask and would be answered using information from the chunk. For example, if the given context was a Wikipedia paragraph about the United States, an example question could be 'How many states are in the United States?'.The questions should be able to be answered in a few words or less. Include only the questions in your response." % (x)},
        {"role": "user", "content": "You are a synthetic question-answer pair generator. Given a chunk of context about some topic(s), generate %s example questions a user could ask and would be answered using information from the chunk.The questions should be able to be answered in a few words or less. Include only the questions in your response." % (x) + str(chunk)}
    ]
    messages = tokenizer.apply_chat_template(messages, tokenize=False, return_attention_mask=False)
    model_inputs = tokenizer(messages, return_tensors='pt').input_ids.cuda()
    outputs = model_chat.generate(model_inputs,
                                      max_new_tokens=2048, 
                                      do_sample=True,
                                      pad_token_id=tokenizer.eos_token_id)
    inputs = model_inputs
    assistant_output = tokenizer.decode(outputs[0][len(inputs[0]):], add_special_tokens=False)
    print("You are a synthetic question-answer pair generator. Given a chunk of context about some topic(s), generate %s example questions a user could ask and would be answered using information from the chunk.The questions should be able to be answered in a few words or less. Include only the questions in your response." % (x) + str(chunk))
    print('assistant output:\n',assistant_output)
    queries = clean_output(assistant_output)
    print('\n Query: \n',queries) #DEBUG STATEMENT 
    # queries = [strip_str(q) for q in assistant_output]
    # queries = [q for q in queries if any(c.isalpha() for c in q)]
    
    return queries 

def clean_output(s: str) -> str:
    """
    Helper function for helping format strings returned by Mistral 7B.
    """
    lines = s.strip().splitlines() 
    clean_lines = [
    re.sub(r"^\s*(\d+\.|\d+\))", "", line.strip())  # Regex to remove numbering format
    if line.strip() else line.strip()
    for line in lines
]
    clean_lines[-1] = clean_lines[-1][:-4]
    
    return clean_lines

def strip_str(s: str) -> str:
    """
    Helper function for helping format strings returned by GPT-4.
    """
    l, r = 0, len(s)-1
    beg_found = False
    for i in range(len(s)):
        if s[i].isalpha():
            if not beg_found:
                l = i
                beg_found = True
            else:
                r = i 
    r += 2
    return s[l:min(r, len(s))]

def encode_question(question: str, api: Any) -> list[str]:
    """
    Encode multiple prompt instructions into a single string for the `api` case.
    """
    prompts = []
        
    prompt = question + "\nWrite a python program to call API in " + str(api) + ".\n\nThe answer should follow the format: <<<domain>>> $DOMAIN \n, <<<api_call>>>: $API_CALL \n, <<<api_provider>>>: $API_PROVIDER \n, <<<explanation>>>: $EXPLANATION \n, <<<code>>>: $CODE}. Here are the requirements:\n \n2. The $DOMAIN should be the domain of the API ('N/A' if unknown). The $API_CALL should have only 1 line of code that calls api.\n3. The $API_PROVIDER should be the programming framework used.\n4. $EXPLANATION should be a numbered, step-by-step explanation.\n5. The $CODE is the python code.\n6. Do not repeat the format in your answer."
    prompts.append({"role": "system", "content": "You are a helpful API writer who can write APIs based on requirements."})
    prompts.append({"role": "user", "content": prompt})
    return prompts

def encode_question_gen(question: str, chunk: Any) -> list[str]:
    """
    Encode multiple prompt instructions into a single string for the general case (`pdf`, `json`, or `txt`).
    """
    
    prompts = []
        
    prompt = """
        Question: {question}\nContext: {context}\n
        Answer this question using the information given in the context above. Here is things to pay attention to: 
        - First provide step-by-step reasoning on how to answer the question. 
        - In the reasoning, if you need to copy paste some sentences from the context, include them in ##begin_quote## and ##end_quote##. This would mean that things outside of ##begin_quote## and ##end_quote## are not directly copy paste from the context. 
        - End your response with final answer in the form <ANSWER>: $answer, the answer should be succint. It is necessary before your anwser you put '<ANWSER>'.
    """.format(question=question, context=str(chunk))
    # prompts.append({"role": "system", "content": "You are a helpful question answerer who can provide an answer given a question and relevant context."})
    prompts.append({"role": "user", "content":"You are a helpful question answerer who can provide an answer given a question and relevant context."+ prompt})
    return prompts

def generate_label(question: str, context: Any, doctype: DocType = "pdf") -> str :
    """
    Generates the label / answer to `question` using `context` and GPT-4.
    """
    question = encode_question(question, context) if doctype == "api" else encode_question_gen(question, context)

    messages = tokenizer.apply_chat_template(question, tokenize=False, return_attention_mask=False)
    model_inputs = tokenizer(messages, return_tensors='pt').input_ids.cuda()
    outputs = model_chat.generate(model_inputs,
                                      max_new_tokens=2048, 
                                      do_sample=True,
                                      pad_token_id=tokenizer.eos_token_id)
    inputs = model_inputs
    response = tokenizer.decode(outputs[0][len(inputs[0]):], add_special_tokens=False)
    print('\nLabel:\n',response)   # DEBUG STATEMENT 
    return response

def add_chunk_to_dataset(
    chunks: list[str], 
    chunk: str, 
    doctype: DocType = "api", 
    x: int = 5, 
    num_distract: int = 3, 
    p: float = 1.0
) -> None:
    """
    Given a chunk, create {Q, A, D} triplets and add them to the dataset.
    """
    global ds
    i = chunks.index(chunk)
    qs = generate_instructions(chunk, x) if doctype == "api" else generate_instructions_gen(chunk, x)
    for q in qs:
        datapt = {
            "id": None,
            "type": None,
            "question": None,
            "context": None,
            "oracle_context": None,
            "cot_answer": None
        }

        datapt["id"] = f"seed_task_{0 if not ds else ds.num_rows}"
        datapt["type"] = "api call" if doctype == "api" else "general"
        datapt["question"] = q

        # add num_distract distractor docs
        docs = [chunk]
        indices = list(range(0, len(chunks)))
        indices.remove(i)
        for j in random.sample(indices, num_distract):
            docs.append(chunks[j])
        # decides whether to add oracle document
        oracle = random.uniform(0, 1) < p
        if not oracle:
            docs[0] = chunks[random.sample(indices, 1)[0]]
        random.shuffle(docs)

        d = {
            "title": [],
            "sentences": []
        }

        d["title"].append(["placeholder_title"]*(num_distract+1))
        d["sentences"].append(docs)
        datapt["context"] = d
        datapt["oracle_context"] = chunk

        # add answer to q
        datapt["cot_answer"] = generate_label(q, chunk, doctype) 

        # construct model instruction 
        context = ""
        for doc in docs:
            context += "<DOCUMENT>" + str(doc) + "</DOCUMENT>\n"
        context += q
        datapt["instruction"] = context

        # add to dataset
        if not ds:
            # init ds
            datapt["id"] = [datapt["id"]]
            datapt["type"] = [datapt["type"]]
            datapt["question"] = [datapt["question"]]
            datapt["context"] = [datapt["context"]]
            datapt["oracle_context"] = [datapt["oracle_context"]]
            datapt["cot_answer"] = [datapt["cot_answer"]]
            datapt["instruction"] = [datapt["instruction"]]
            ds = Dataset.from_dict(datapt)
        else:
            ds = ds.add_item(datapt)


if __name__ == "__main__":
    # run code
    args = get_args()
    
    OPENAPI_API_KEY = args.openai_key

    client = OpenAI(
        api_key=OPENAPI_API_KEY,
    )

    CHUNK_SIZE = args.chunk_size
    NUM_DISTRACT_DOCS = args.distractors

    chunks = get_chunks(args.datapath, args.doctype, CHUNK_SIZE, OPENAPI_API_KEY)

    ds = None

    for i in tqdm(range(0,len(chunks))):
        add_chunk_to_dataset(chunks, chunks[i], args.doctype, args.questions, NUM_DISTRACT_DOCS)
    
    # Save as .arrow format
    ds.save_to_disk(args.output)
    
    # Save as .jsonl format
    ds.to_json(args.output + ".jsonl")
