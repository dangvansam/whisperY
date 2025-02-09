import os
from textwrap import dedent
from venv import logger
import warnings
from typing import Generator, List, Union, Optional, NamedTuple

import ctranslate2
import faster_whisper
import numpy as np
import torch
from tqdm import tqdm
from transformers import Pipeline
from transformers import AutoTokenizer
from transformers.pipelines.pt_utils import PipelineIterator
from transformers import WhisperForConditionalGeneration, AutoProcessor
import whisper

from .audio import N_SAMPLES, SAMPLE_RATE, load_audio, log_mel_spectrogram, pad_or_trim
from .vad import load_vad_model, merge_chunks
from .types import TranscriptionResult, SingleSegment

def find_numeral_symbol_tokens(tokenizer):
    numeral_symbol_tokens = []
    for i in range(tokenizer.eot):
        token = tokenizer.decode([i]).removeprefix(" ")
        has_numeral_symbol = any(c in "0123456789%$£" for c in token)
        if has_numeral_symbol:
            numeral_symbol_tokens.append(i)
    return numeral_symbol_tokens

class WhisperModel(faster_whisper.WhisperModel):
    '''
    FasterWhisperModel provides batched inference for faster-whisper.
    Currently only works in non-timestamp mode and fixed prompt for all samples in batch.
    '''

    def generate_segment_batched(
        self,
        features: np.ndarray,
        tokenizer: Tokenizer,
        options: TranscriptionOptions,
        encoder_output=None,
    ):
        batch_size = features.shape[0]
        all_tokens = []
        prompt_reset_since = 0
        if options.initial_prompt is not None:
            initial_prompt = " " + options.initial_prompt.strip()
            initial_prompt_tokens = tokenizer.encode(initial_prompt)
            all_tokens.extend(initial_prompt_tokens)
        previous_tokens = all_tokens[prompt_reset_since:]
        prompt = self.get_prompt(
            tokenizer,
            previous_tokens,
            without_timestamps=options.without_timestamps,
            prefix=options.prefix,
        )

        encoder_output = self.encode(features)
        max_initial_timestamp_index = int(
            round(options.max_initial_timestamp / self.time_precision)
        )
        results = self.model.generate(
                encoder_output,
                [prompt] * batch_size,
                beam_size=options.beam_size,
                patience=options.patience,
                length_penalty=options.length_penalty,
                max_length=self.max_length,
                suppress_blank=options.suppress_blank,
                suppress_tokens=options.suppress_tokens,
                return_scores=True,
                return_no_speech_prob=True
            )
        output = []
        suppress_low = [
            "Thank you", "Thanks for", "ike and ", "Bye.", "Bye!", "Bye bye!", "lease sub", "The end."
        ]
        suppress_high = [
            "ubscribe", "my channel", "the channel", "our channel", "ollow me on", "for watching", "hank you for watching",
            "for your viewing", "r viewing", "Amara", "next video", "full video", "ranslation by", "ranslated by",
            "ee you next week"
        ]
        for r in results:
            seq_len = len(r.sequences_ids[0])
            cum_logprob = r.scores[0] * (seq_len**options.length_penalty)
            avg_logprob = cum_logprob / (seq_len + 1)
            text = tokenizer.decode(r.sequences_ids[0])
            print(avg_logprob, r.no_speech_prob, text)
            for s in suppress_low:
                if s in text:
                    avg_logprob -= 0.15
            for s in suppress_high:
                if s in text:
                    avg_logprob -= 0.35

            if avg_logprob < -1.0 or r.no_speech_prob > 0.7:
                print(f"{avg_logprob:.2f}, {r.no_speech_prob:.2f}, {text}")
                continue
            output.append(
                dict(
                    avg_logprob=avg_logprob,
                    no_speech_prob=r.no_speech_prob,
                    # tokens=r.sequences_ids[0],
                    text=text
                )
            )

        results = output
        # print("results:", results)
        
        # subs = []
        # segment_info = []
        # sub_index = 0

        # for r in tqdm(result):
        #     # if r["start"] > chunks[i][-1]["chunk_end"]:
        #     #     continue
        #     for s in self.suppress_low:
        #         if s in r["text"]:
        #             r["avg_logprob"] -= 0.15
        #     for s in self.suppress_high:
        #         if s in r["text"]:
        #             r["avg_logprob"] -= 0.35
        #     del r["tokens"]
            
        #     segment_info.append(r)
            
        #     if r["avg_logprob"] < -1.0 or r["no_speech_prob"] > 0.7:
        #         print(f"{r['avg_logprob']:.2f}, {r['no_speech_prob']:.2f}, {r['text']}")
        #         continue
        #     start = r["start"]

            # for j in range(len(chunks[i])):
            #     if r["start"] >= chunks[i][j]["chunk_start"] and r["start"] <= chunks[i][j]["chunk_end"]:
            #         start = r["start"] + chunks[i][j]["offset"]
            #         break

            # if len(subs) > 0:
            #     last_end = subs[-1]["end"]
            #     if last_end > start:
            #         subs[-1]["end"] = start

            # end = chunks[i][-1]["end"] + 0.5
            # for j in range(len(chunks[i])):
            #     if r["end"] >= chunks[i][j]["chunk_start"] and r["end"] <= chunks[i][j]["chunk_end"]:
            #         end = r["end"] + chunks[i][j]["offset"]
            #         break
            # subs.append(
            #     {
            #         "start": start,
            #         "end": end,
            #         "text": r["text"].strip()
            #     }
            # )
            # sub_index += 1

        text = [x['text'] for x in results]
        return text

    def encode(self, features: np.ndarray) -> ctranslate2.StorageView:
        # When the model is running on multiple GPUs, the encoder output should be moved
        # to the CPU since we don't know which GPU will handle the next job.
        to_cpu = self.model.device == "cuda" and len(self.model.device_index) > 1
        # unsqueeze if batch size = 1
        if len(features.shape) == 2:
            features = np.expand_dims(features, 0)
        features = get_ctranslate2_storage(features)

        return self.model.encode(features, to_cpu=to_cpu)

class HuggingfaceWhisperModel():
    def __init__(self, model_name="openai/whisper-large-v3", device="cuda"):
        """Initialize HuggingFace Whisper model.
        
        Args:
            model_name (str): Name of Whisper model to load
            device (str): Device to run model on ('cuda' or 'cpu')
        """
        self.device = device
        self.processor = AutoProcessor.from_pretrained(model_name)
        self.model = WhisperForConditionalGeneration.from_pretrained(
            pretrained_model_name_or_path=model_name, 
            torch_dtype=torch.float16 if device == "cuda" else torch.float32
        ).to(device)

    def transcribe(self, inputs):
        """Transcribe a batch of audio files.
        
        Args:
            audio (list): List of audios
            
        Returns:
            list: Transcribed text for each audio file
        """
        # Process inputs
        inputs = self.processor(
            inputs, 
            return_tensors="pt",
            truncation=False,
            padding="longest", 
            return_attention_mask=True,
            sampling_rate=16000
        )
        inputs = inputs.to(self.device)

        # Generate transcriptions
        result = self.model.generate(
            **inputs,
            condition_on_prev_tokens=False,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            logprob_threshold=-1.0,
            compression_ratio_threshold=1.35,
            return_timestamps=True
        )

        # Decode results
        transcriptions = self.processor.batch_decode(result, skip_special_tokens=True)
        print(transcriptions)
        return transcriptions

class FasterWhisperPipeline(Pipeline):
    """
    Huggingface Pipeline wrapper for FasterWhisperModel.
    """
    # TODO:
    # - add support for timestamp mode
    # - add support for custom inference kwargs

    def __init__(
        self,
        model: WhisperModel,
        vad,
        vad_params: dict,
        options: TranscriptionOptions,
        tokenizer: Optional[Tokenizer] = None,
        device: Union[int, str, "torch.device"] = -1,
        framework="pt",
        language: Optional[str] = None,
        suppress_numerals: bool = False,
        **kwargs,
    ):
        self.model: whisper.model.Whisper | WhisperModel | HuggingfaceWhisperModel = model
        self.tokenizer = tokenizer
        self.options = options
        self.preset_language = language
        self.suppress_numerals = suppress_numerals
        self._batch_size = kwargs.pop("batch_size", None)
        self._num_workers = 1
        self._preprocess_params, self._forward_params, self._postprocess_params = self._sanitize_parameters(**kwargs)
        self.call_count = 0
        self.framework = framework
        if self.framework == "pt":
            if isinstance(device, torch.device):
                self.device = device
            elif isinstance(device, str):
                self.device = torch.device(device)
            elif device < 0:
                self.device = torch.device("cpu")
            else:
                self.device = torch.device(f"cuda:{device}")
        else:
            self.device = device

        super(Pipeline, self).__init__()
        self.vad_model = vad
        self._vad_params = vad_params

    def _sanitize_parameters(self, **kwargs):
        preprocess_kwargs = {}
        if "tokenizer" in kwargs:
            preprocess_kwargs["maybe_arg"] = kwargs["maybe_arg"]
        return preprocess_kwargs, {}, {}

    def preprocess(self, audio):
        audio = audio['inputs']
        if isinstance(self.model, whisper.model.Whisper):
            audio = pad_or_trim(audio)
            return {'inputs': audio}
        elif isinstance(self.model, HuggingfaceWhisperModel):
            return audio
        else:
            model_n_mels = self.model.feat_kwargs.get("feature_size")
            features = log_mel_spectrogram(
                audio,
                n_mels=model_n_mels if model_n_mels is not None else 80,
                padding=N_SAMPLES - audio.shape[0],
            )
            return {'inputs': features}

    def _forward(self, model_inputs):
        if isinstance(self.model, whisper.model.Whisper):
            suppress_low = [
                "Thanks for", "ike and ", "Bye.", "Bye!", "Bye bye!", "lease sub", "The end."
            ]
            suppress_high = [
                "ubscribe", "my channel", "the channel", "our channel", "ollow me on", "for watching", "hank you for watching",
                "Hãy subscribe", "Ghiền Mì Gõ", "không bỏ lỡ những video hấp dẫn", "kênh La La School"
            ]
            outputs = []
            for i in range(len(model_inputs['inputs'])):
                result = self.model.transcribe(
                    verbose=False,
                    audio=model_inputs['inputs'][i],
                    task="transcribe",
                    language="vi",
                    initial_prompt="",
                    logprob_threshold = -1.0,
                    no_speech_threshold = 0.6,
                    condition_on_previous_text=False,
                    hallucination_silence_threshold=2,
                    temperature=0,
                    beam_size=self.options.beam_size,
                    patience=self.options.patience,
                    length_penalty=self.options.length_penalty,
                    # compression_ratio_threshold=1
                )
                result = result["segments"]
                output = []
                for r in result:
                    for s in suppress_low:
                        if s in r["text"]:
                            r["avg_logprob"] -= 0.15
                    for s in suppress_high:
                        if s in r["text"]:
                            r["avg_logprob"] -= 0.35

                    if (r["avg_logprob"] < -0.5 and r["compression_ratio"] < 1) \
                        or (r["avg_logprob"] < -0.5 and r["compression_ratio"] > 2) \
                        or r["no_speech_prob"] > 0.7 \
                        or r["text"].strip() == "":
                        print(dedent(f"""
                            text: {r['text']}
                            avg_logprob: {r['avg_logprob']:.2f}
                            no_speech_prob: {r['no_speech_prob']:.2f}
                            compression_ratio: {r['compression_ratio']}
                        """))
                        continue
                    output.append(r["text"])
                output = " ".join(output)
                output = " ".join(output.split())
                outputs.append(output)
        elif isinstance(self.model, HuggingfaceWhisperModel):
            outputs = self.model.transcribe(model_inputs)
        else:
            outputs = self.model.generate_segment_batched(model_inputs['inputs'], self.tokenizer, self.options)
        return {'text': outputs}

    def postprocess(self, model_outputs):
        return model_outputs

    def get_iterator(
        self,
        inputs,
        num_workers: int,
        batch_size: int,
        preprocess_params: dict,
        forward_params: dict,
        postprocess_params: dict,
    ):
        dataset = PipelineIterator(inputs, self.preprocess, preprocess_params)
        if "TOKENIZERS_PARALLELISM" not in os.environ:
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
        # TODO hack by collating feature_extractor and image_processor

        def stack(items):
            if isinstance(self.model, HuggingfaceWhisperModel):
                return items
            else:
                return {'inputs': torch.stack([x['inputs'] for x in items])}
        dataloader = torch.utils.data.DataLoader(dataset, num_workers=num_workers, batch_size=batch_size, collate_fn=stack)
        model_iterator = PipelineIterator(dataloader, self.forward, forward_params, loader_batch_size=batch_size)
        final_iterator = PipelineIterator(model_iterator, self.postprocess, postprocess_params)
        return final_iterator

    def transcribe(
        self, audio: Union[str, np.ndarray], batch_size=None, num_workers=0, language='vi', task='transcribe', chunk_size=30, print_progress = False, combined_progress=False
    ):
        if isinstance(audio, str):
            audio = load_audio(audio)

        def data(audio, segments):
            for seg in segments:
                f1 = int(seg['start'] * SAMPLE_RATE)
                f2 = int(seg['end'] * SAMPLE_RATE)
                yield {'inputs': torch.from_numpy(audio[f1:f2])}

        # Pre-process audio and merge chunks as defined by the respective VAD child class 
        # In case vad_model is manually assigned (see 'load_model') follow the functionality of pyannote toolkit
        if issubclass(type(self.vad_model), Vad):
            waveform = self.vad_model.preprocess_audio(audio)
            merge_chunks =  self.vad_model.merge_chunks
        else:
            waveform = Pyannote.preprocess_audio(audio)
            merge_chunks = Pyannote.merge_chunks

        vad_segments = self.vad_model({"waveform": waveform, "sample_rate": SAMPLE_RATE})
        vad_segments = merge_chunks(
            vad_segments,
            chunk_size,
            onset=self._vad_params["vad_onset"],
            offset=self._vad_params["vad_offset"],
        )
        if isinstance(self.model, WhisperModel):
            if self.tokenizer is None:
                language = language or self.detect_language(audio)
                task = task or "transcribe"
                self.tokenizer = faster_whisper.tokenizer.Tokenizer(self.model.hf_tokenizer,
                                                                    self.model.model.is_multilingual, task=task,
                                                                    language=language)
            else:
                language = language or self.tokenizer.language_code
                task = task or self.tokenizer.task
                if task != self.tokenizer.task or language != self.tokenizer.language_code:
                    self.tokenizer = faster_whisper.tokenizer.Tokenizer(self.model.hf_tokenizer,
                                                                        self.model.model.is_multilingual, task=task,
                                                                        language=language)
                
        if self.suppress_numerals:
            previous_suppress_tokens = self.options.suppress_tokens
            numeral_symbol_tokens = find_numeral_symbol_tokens(self.tokenizer)
            print(f"Suppressing numeral and symbol tokens")
            new_suppressed_tokens = numeral_symbol_tokens + self.options.suppress_tokens
            new_suppressed_tokens = list(set(new_suppressed_tokens))
            self.options = replace(self.options, suppress_tokens=new_suppressed_tokens)

        segments: List[SingleSegment] = []
        batch_size = batch_size or self._batch_size
        total_segments = len(vad_segments)
        for idx, out in enumerate(self.__call__(data(audio, vad_segments), batch_size=batch_size, num_workers=num_workers)):
            if print_progress:
                base_progress = ((idx + 1) / total_segments) * 100
                percent_complete = base_progress / 2 if combined_progress else base_progress
                print(f"Progress: {percent_complete:.2f}%...")
            text = out['text']
            if batch_size in [0, 1, None]:
                text = text[0]
            segment = {
                "text": text,
                "start": round(vad_segments[idx]['start'], 3),
                "end": round(vad_segments[idx]['end'], 3)
            }
            segments.append(segment)
            yield text

        # revert the tokenizer if multilingual inference is enabled
        if self.preset_language is None:
            self.tokenizer = None

        # revert suppressed tokens if suppress_numerals is enabled
        if self.suppress_numerals:
            self.options = replace(self.options, suppress_tokens=previous_suppress_tokens)

        return {"segments": segments, "language": language}

    def detect_language(self, audio: np.ndarray):
        if audio.shape[0] < N_SAMPLES:
            print("Warning: audio is shorter than 30s, language detection may be inaccurate.")
        model_n_mels = self.model.feat_kwargs.get("feature_size")
        segment = log_mel_spectrogram(audio[: N_SAMPLES],
                                      n_mels=model_n_mels if model_n_mels is not None else 80,
                                      padding=0 if audio.shape[0] >= N_SAMPLES else N_SAMPLES - audio.shape[0])
        encoder_output = self.model.encode(segment)
        results = self.model.model.detect_language(encoder_output)
        language_token, language_probability = results[0][0]
        language = language_token[2:-2]
        print(f"Detected language: {language} ({language_probability:.2f}) in first 30s of audio...")
        return language


def load_model(
    whisper_arch: str,
    device: str,
    device_index=0,
    compute_type="float16",
    asr_options: Optional[dict] = None,
    language: Optional[str] = None,
    vad_model: Optional[Vad]= None,
    vad_method: Optional[str] = "pyannote",
    vad_options: Optional[dict] = None,
    model: Optional[WhisperModel] = None,
    task="transcribe",
    download_root: Optional[str] = None,
    local_files_only=False,
    threads=4,
) -> FasterWhisperPipeline:
    """Load a Whisper model for inference.
    Args:
        whisper_arch - The name of the Whisper model to load.
        device - The device to load the model on.
        compute_type - The compute type to use for the model.
        vad_method - The vad method to use. vad_model has higher priority if is not None.
        options - A dictionary of options to use for the model.
        language - The language of the model. (use English for now)
        model - The WhisperModel instance to use.
        download_root - The root directory to download the model to.
        local_files_only - If `True`, avoid downloading the file and return the path to the local cached file if it exists.
        threads - The number of cpu threads to use per worker, e.g. will be multiplied by num workers.
    Returns:
        A Whisper pipeline.
    """

    if whisper_arch.endswith(".en"):
        language = "en"

    model = whisper.load_model(whisper_arch)
    # model = HuggingfaceWhisperModel('openai/whisper-large-v3', device)
    tokenizer = AutoTokenizer.from_pretrained('openai/whisper-large-v3')

    # model2 = WhisperModel(whisper_arch,
    #                      device=device,
    #                      device_index=device_index,
    #                      compute_type=compute_type,
    #                      download_root=download_root,
    #                      cpu_threads=threads)
    # model = model2
    # if language is not None:
    #     tokenizer = faster_whisper.tokenizer.Tokenizer(model2.hf_tokenizer, model2.model.is_multilingual, task=task, language=language)
    # else:
    #     print("No language specified, language will be first be detected for each audio file (increases inference time).")
    #     tokenizer = None

    default_asr_options =  {
        "beam_size": 5,
        "best_of": 5,
        "patience": 1,
        "length_penalty": 1,
        "repetition_penalty": 1,
        "no_repeat_ngram_size": 0,
        "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "condition_on_previous_text": False,
        "prompt_reset_on_temperature": 0.5,
        "initial_prompt": None,
        "prefix": None,
        "suppress_blank": True,
        "suppress_tokens": [-1],
        "without_timestamps": True,
        "max_initial_timestamp": 0.0,
        "word_timestamps": False,
        "prepend_punctuations": "\"'“¿([{-",
        "append_punctuations": "\"'.。,，!！?？:：”)]}、",
        "multilingual": model.model.is_multilingual,
        "suppress_numerals": False,
        "max_new_tokens": None,
        "clip_timestamps": None,
        "hallucination_silence_threshold": None,
        "hotwords": None,
    }

    if asr_options is not None:
        default_asr_options.update(asr_options)

    suppress_numerals = default_asr_options["suppress_numerals"]
    del default_asr_options["suppress_numerals"]

    default_asr_options = TranscriptionOptions(**default_asr_options)

    default_vad_options = {
        "vad_onset": 0.8,
        "vad_offset": 0.5
    }

    if vad_options is not None:
        default_vad_options.update(vad_options)

    # Note: manually assigned vad_model has higher priority than vad_method!
    if vad_model is not None:
        print("Use manually assigned vad_model. vad_method is ignored.")
        vad_model = vad_model
    else:
        if vad_method == "silero":
            vad_model = Silero(**default_vad_options)
        elif vad_method == "pyannote":
            vad_model = Pyannote(torch.device(device), use_auth_token=None, **default_vad_options)
        else:
            raise ValueError(f"Invalid vad_method: {vad_method}")

    return FasterWhisperPipeline(
        model=model,
        vad=vad_model,
        options=default_asr_options,
        tokenizer=tokenizer,
        language=language,
        suppress_numerals=suppress_numerals,
        vad_params=default_vad_options,
    )
