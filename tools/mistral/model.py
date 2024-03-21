
from megatron.core.enums import ModelType


model_type = ModelType.encoder_or_decoder


def get_hf_model(model_path):
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(model_path, device_map="cpu", trust_remote_code=True)
    return model


def get_mg_model(dtype, pre_process, post_process):
    from pretrain_gpt import model_provider
    model = model_provider(pre_process, post_process).to(dtype)
    return model
