import random

import numpy as np
import torch
from datasets import load_dataset
from gptqmodel import GPTQModel, QuantizeConfig
from transformers import TextGenerationPipeline

pretrained_model_id = "gpt2-xl"
quantized_model_id = "gpt2-large-4bit-128g"


# os.makedirs(quantized_model_dir, exist_ok=True)
def get_wikitext2(nsamples, seed, seqlen, tokenizer):
    # set seed
    random.seed(seed)
    np.random.seed(seed)
    torch.random.manual_seed(seed)

    # load dataset and preprocess
    traindata = load_dataset("wikitext", "wikitext-2-raw-v1", split="train")
    testdata = load_dataset("wikitext", "wikitext-2-raw-v1", split="test")
    trainenc = tokenizer("\n\n".join(traindata["text"]), return_tensors="pt")
    testenc = tokenizer("\n\n".join(testdata["text"]), return_tensors="pt")

    traindataset = []
    for _ in range(nsamples):
        i = random.randint(0, trainenc.input_ids.shape[1] - seqlen - 1)
        j = i + seqlen
        inp = trainenc.input_ids[:, i:j]
        attention_mask = torch.ones_like(inp)
        traindataset.append({"input_ids": inp, "attention_mask": attention_mask})
    return traindataset, testenc


def main():
    from transformers import AutoTokenizer

    try:
        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_id, use_fast=False)
    except Exception:
        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_id, use_fast=True)

    # load un-quantized model, the model will always be force loaded into cpu
    quantize_config = QuantizeConfig(
        bits=4,  # quantize model to 4-bit
        group_size=128,  # it is recommended to set the value to 128
    )

    # get model maximum sequence length
    model = GPTQModel.from_pretrained(pretrained_model_id, quantize_config, torch_dtype=torch.float16)
    model_config = model.config.to_dict()
    seq_len_keys = ["max_position_embeddings", "seq_length", "n_positions"]
    if any(k in model_config for k in seq_len_keys):
        for key in seq_len_keys:
            if key in model_config:
                model.seqlen = model_config[key]
                break
    else:
        print("can't get model's sequence length from model config, will set to 2048.")
        model.seqlen = 2048

    # load train dataset for quantize
    traindataset, testenc = get_wikitext2(128, 0, model.seqlen, tokenizer)

    # quantize model, the calibration_dataset should be list of dict whose keys contains "input_ids" and "attention_mask"
    # with value under torch.LongTensor type.
    model.quantize(traindataset)

    # save quantized model
    model.save_quantized(quantized_model_id)

    # save quantized model using safetensors
    model.save_quantized(quantized_model_id, use_safetensors=True)

    # load quantized model, currently only support cpu or single gpu
    model = GPTQModel.from_quantized(quantized_model_id, device="cuda:0")

    # inference with model.generate
    print(tokenizer.decode(model.generate(**tokenizer("test is", return_tensors="pt").to("cuda:0"))[0]))

    # or you can also use pipeline
    pipeline = TextGenerationPipeline(model=model, tokenizer=tokenizer)
    print(pipeline("test is")[0]["generated_text"])


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        level=logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    main()
