import json
import logging
from dataclasses import dataclass, field, fields
from os.path import isdir, join
from typing import Any, Dict, Optional, Tuple

from packaging import version
from transformers.utils.hub import cached_file

logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.propagate = False
logger.addHandler(handler)
logger.setLevel(logging.INFO)

FORMAT_FIELD_CODE = "format"
FORMAT_FIELD_JSON = "checkpoint_format"
FORMAT_FIELD_COMPAT_MARLIN = "is_marlin_format"
QUANT_METHOD_FIELD = "quant_method"
QUANT_CONFIG_FILENAME = "quantize_config.json"
QUANT_CONFIG_FILENAME_COMPAT = [QUANT_CONFIG_FILENAME, "quant_config.json", "config.json"]

MIN_VERSION_WITH_V2 = "0.9.0"

META_FIELD = "meta"
# quantizer is the tool that did the quantization
META_FIELD_QUANTIZER = "quantizer"
# packer is the tool that packed the weights post quantization
META_FIELD_PACKER = "packer"

META_QUANTIZER_GPTQMODEL = "gptqmodel"


# saved formats
class FORMAT:
    GPTQ = "gptq"
    # v2 format fixed sym = False quantization
    GPTQ_V2 = "gptq_v2"
    MARLIN = "marlin"
    BITBLAS = "bitblas"
    QBITS = "qbits"


# quant methods
class QUANT_METHOD:
    GPTQ = "gptq"
    AUTO_ROUND = "auto_round"


QUANT_METHOD_FORMAT_MAPPING = {
    QUANT_METHOD.GPTQ: {
        FORMAT.GPTQ,
        FORMAT.GPTQ_V2,
        FORMAT.MARLIN,
        FORMAT.BITBLAS,
        FORMAT.QBITS,
    },
    QUANT_METHOD.AUTO_ROUND: {
        FORMAT.GPTQ,
    }
}

# inference only methods should go here
QUANTIZE_BLACK_LIST = {}

# compat
QUANT_CONFIG_ARG_SYNONYMS = {
    "w_bit": "bits",
    "q_group_size": "group_size",
    # map format field (checkpoint_format) to class/code (format)
    FORMAT_FIELD_JSON: FORMAT_FIELD_CODE,
}


@dataclass
class QuantizeConfig():
    bits: int = field(default=4, metadata={"choices": [2, 3, 4, 8]})
    group_size: int = field(default=-1)
    damp_percent: float = field(default=0.01)
    desc_act: bool = field(default=True)
    static_groups: bool = field(default=False)
    sym: bool = field(default=True)
    true_sequential: bool = field(default=True)
    lm_head: bool = field(default=False)
    quant_method: str = field(default=QUANT_METHOD.GPTQ)
    # default to gptq v1 format for maximum compat with 3rd party inference libs with minimal loss vs v2
    # if you inference with gptqmodel, save to gptq_v2 format for best result
    format: FORMAT = field(default=FORMAT.GPTQ)

    # TODO: remove
    model_name_or_path: Optional[str] = field(default=None)
    model_file_base_name: Optional[str] = field(default=None)

    # properties that do not directly contributes to quantization or quant inference should be placed in meta
    # i.e. quantizer tool (producer) + version, timestamp, entity who made the quant, etc
    meta: Optional[Dict] = field(default=None)

    def __post_init__(self):
        fields_info = fields(self)

        # validate quant method and format is matched
        valid_formats = QUANT_METHOD_FORMAT_MAPPING.get(self.quant_method, None)
        if valid_formats is None:
            raise ValueError(f"Unsupported quantization method: {self.quant_method}")

        if self.format not in valid_formats:
            raise ValueError(
                f"The checkpoint format used is {self.format}, and the quantization method is {self.quant_method}. "
            )

        if self.bits not in fields_info[0].metadata["choices"]:
            raise ValueError(f"only support quantize to {fields_info[0].metadata['choices']} bits.")

        if self.group_size != -1 and self.group_size <= 0:
            raise ValueError("unless equal to -1, group_size must greater then 0.")

        if not (0 < self.damp_percent < 1):
            raise ValueError("damp_percent must between 0 and 1.")

        # validate meta
        if self.meta is not None:
            if not isinstance(self.meta, dict):
                raise ValueError("meta must be a dictionary")
            for key, value in self.meta.items():
                if not isinstance(key, str):
                    raise ValueError("Keys in the meta dictionary must be strings")
        else:
            self.meta = {}

    def meta_set(self, key: str, value: Any):
        self.meta[key] = value

    def meta_get(self, key: str) -> Any:
        return self.meta.get(key)

    # versionable is a meta.property that pairs value with version i.e "value:1.0.0"
    def meta_set_versionable(self, key: str, value: str, version: str):
        self.meta_set(key, f"{value}:{version}")

    # versionable is a meta.property that pairs value with version i.e "value:1.0.0"
    def meta_get_versionable(self, key: str) -> Tuple[str, str]:
        val = self.meta_get(key)
        if val is None:
            return None, None
        parts = val.split(":")
        return parts[0].lower(), parts[1].lower() if len(parts) >= 2 else None

    # is quantized model quantized or packed by gptqmodel version with v2 format code
    def is_quantized_or_packed_by_v2(self) -> bool:
        # check meta.quantizer
        producer, _version = self.meta_get_versionable(META_FIELD_QUANTIZER)
        by_v2 = (producer == META_QUANTIZER_GPTQMODEL) and (version.parse(_version) >= version.parse(MIN_VERSION_WITH_V2))

        # fallback to meta.packer
        if not by_v2:
            producer, _version = self.meta_get_versionable(META_FIELD_PACKER)
            by_v2 = producer == META_QUANTIZER_GPTQMODEL and version.parse(_version) >= version.parse(
                MIN_VERSION_WITH_V2
            )

        return by_v2

    def save_pretrained(self, save_dir: str, **kwargs):
        with open(join(save_dir, QUANT_CONFIG_FILENAME), "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    # normalize quant config for compat and also performs validation
    def from_quant_config(cls, quantize_cfg, format: str = None):
        valid_formats = {FORMAT.GPTQ, FORMAT.GPTQ_V2, FORMAT.MARLIN, FORMAT.BITBLAS}
        format_auto_inferred = False
        # compat: format can be passed in via from_quantized() if field missing from json
        if format:
            if format not in valid_formats:
                raise ValueError(f"Unknown quantization checkpoint format: {format}.")
            if quantize_cfg.get(FORMAT_FIELD_JSON):
                raise ValueError("Conflict: quantization format is passed in and also exists in model config.")
        # compat: warn if checkpoint_format is missing
        elif quantize_cfg.get(FORMAT_FIELD_JSON) is None:
            format_auto_inferred = True

        field_names = [field.name for field in fields(cls)]

        normalized = {
            QUANT_METHOD_FIELD: QUANT_METHOD.GPTQ,
            # compat: default to gptq(v1) when loading models
            FORMAT_FIELD_CODE: format if format else FORMAT.GPTQ,
        }
        for key, val in quantize_cfg.items():
            key = key.lower()

            # remap keys according to compat map
            if key in QUANT_CONFIG_ARG_SYNONYMS and QUANT_CONFIG_ARG_SYNONYMS[key] in field_names:
                key = QUANT_CONFIG_ARG_SYNONYMS[key]

            if key == FORMAT_FIELD_JSON:
                val = val.lower()

                if val in {FORMAT.GPTQ, FORMAT.GPTQ_V2, FORMAT.MARLIN, FORMAT.BITBLAS}:
                    normalized[key] = val
                else:
                    raise ValueError(f"Unknown quantization format: {val}.")
            elif key == QUANT_METHOD_FIELD:
                val = val.lower()
                # compat: some hf models use quant_method=marlin or bitblas
                if val == FORMAT.MARLIN:
                    normalized[FORMAT_FIELD_CODE] = FORMAT.MARLIN
                elif val == FORMAT.BITBLAS:
                    normalized[FORMAT_FIELD_CODE] = FORMAT.BITBLAS
                elif val not in {QUANT_METHOD.GPTQ, QUANT_METHOD.AUTO_ROUND}:
                    raise ValueError(f"Unknown quantization method: {val}.")
                else:
                    normalized[QUANT_METHOD_FIELD] = val
            elif key == FORMAT_FIELD_COMPAT_MARLIN and val:
                normalized[FORMAT_FIELD_CODE] = FORMAT.MARLIN
            elif key in field_names:
                normalized[key] = val
            else:
                logger.info(f"Ignoring unknown parameter in the quantization configuration: {key}.")

        if format_auto_inferred:
            logger.info(f"`{FORMAT_FIELD_JSON}` is missing from the quantization configuration and is automatically inferred to {normalized[FORMAT_FIELD_CODE]}")

        if normalized[FORMAT_FIELD_CODE] in {FORMAT.BITBLAS}:
            # AWQ and Marlin do not reorder the rows.
            normalized["desc_act"] = False

        if "sym" not in normalized:
            logger.warning(
                "The quantization configuration does not contain an entry `sym` (symmetric quantization). "
                "This may result in silent errors. Defaulting to `sym=True`."
            )

        return cls(**normalized)

    @classmethod
    def from_pretrained(cls, save_dir: str, **kwargs):
        # Parameters related to loading from Hugging Face Hub
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        resume_download = kwargs.pop("resume_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", False)
        use_auth_token = kwargs.pop("use_auth_token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        commit_hash = kwargs.pop("_commit_hash", None)
        format = kwargs.pop("format", None)

        transformers_config = False
        for quantize_config_filename in QUANT_CONFIG_FILENAME_COMPAT:
            if isdir(save_dir):  # Local
                resolved_config_file = join(save_dir, quantize_config_filename)
            else:  # Remote
                resolved_config_file = cached_file(
                    save_dir,
                    quantize_config_filename,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    resume_download=resume_download,
                    proxies=proxies,
                    use_auth_token=use_auth_token,
                    revision=revision,
                    local_files_only=local_files_only,
                    subfolder=subfolder,
                    _raise_exceptions_for_missing_entries=False,
                    _raise_exceptions_for_connection_errors=False,
                    _commit_hash=commit_hash,
                )
            if resolved_config_file is not None:
                if quantize_config_filename == "config.json":
                    transformers_config = True
                break

        if resolved_config_file is None:
            raise ValueError(
                "No quantize_config.json, quant_config.json or config.json file was found in the model repository."
            )

        with open(resolved_config_file, "r", encoding="utf-8") as f:
            args_from_json = json.load(f)

            if transformers_config:
                args_from_json = args_from_json["quantization_config"]

            return cls.from_quant_config(args_from_json, format)

    def to_dict(self):
        return {
            "bits": self.bits,
            "group_size": self.group_size,
            "desc_act": self.desc_act,
            "static_groups": self.static_groups,
            "sym": self.sym,
            "lm_head": self.lm_head,
            "damp_percent": self.damp_percent,
            "true_sequential": self.true_sequential,
            # TODO: deprecate?
            "model_name_or_path": self.model_name_or_path,
            "model_file_base_name": self.model_file_base_name,
            QUANT_METHOD_FIELD: self.quant_method,
            FORMAT_FIELD_JSON: self.format,
            META_FIELD: self.meta,
        }

@dataclass
class AutoRoundQuantizeConfig(QuantizeConfig):
    enable_full_range: bool = False  ##for symmetric, TODO support later
    batch_size: int = 1
    amp: bool = True
    lr_scheduler = None
    enable_quanted_input: bool = True
    enable_minmax_tuning: bool = True
    lr: float = None
    minmax_lr: float = None
    low_gpu_mem_usage: bool = False
    iters: int = 200
    seqlen: int = 2048
    sampler: str = "rand"
    seed: int = 42
    nblocks: int = 1
    gradient_accumulate_steps: int = 1
    not_use_best_mse: bool = False
    dynamic_max_gap: int = -1
    data_type: str = "int"  ##only support int for now
    scale_dtype: str = "fp16"
    quant_method: str = QUANT_METHOD.AUTO_ROUND

    def to_dict(self):
        self.meta_set("enable_full_range", self.enable_full_range)
        self.meta_set("batch_size", self.batch_size)
        self.meta_set("amp", self.amp)
        self.meta_set("lr_scheduler", self.lr_scheduler)
        self.meta_set("enable_quanted_input", self.enable_quanted_input)
        self.meta_set("enable_minmax_tuning", self.enable_minmax_tuning)
        self.meta_set("lr", self.lr)
        self.meta_set("minmax_lr", self.minmax_lr)
        self.meta_set("low_gpu_mem_usage", self.low_gpu_mem_usage)
        self.meta_set("iters", self.iters)
        self.meta_set("seqlen", self.seqlen)
        # self.meta_set("nsamples", self.nsamples)
        self.meta_set("sampler", self.sampler)
        self.meta_set("seed", self.seed)
        self.meta_set("nblocks", self.nblocks)
        self.meta_set("gradient_accumulate_steps", self.gradient_accumulate_steps)
        self.meta_set("not_use_best_mse", self.not_use_best_mse)
        self.meta_set("dynamic_max_gap", self.dynamic_max_gap)
        self.meta_set("data_type", self.data_type)
        self.meta_set("scale_dtype", self.scale_dtype)

        return super().to_dict()

# deprecated: will be removed in future update
@dataclass
class BaseQuantizeConfig(QuantizeConfig):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        logging.warning("BaseQuantizeConfig is re-named and pending deprecation. Please use `QuantizeConfig` instead.")
