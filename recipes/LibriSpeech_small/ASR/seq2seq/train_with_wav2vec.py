#!/usr/bin/env/python3
"""Recipe for training a sequence-to-sequence ASR system with librispeech.
The system employs an encoder, a decoder, and an attention mechanism
between them. Decoding is performed with beamsearch coupled with a neural
language model.

To run this recipe, do the following:
> python train.py hparams/train_BPE1000.yaml

With the default hyperparameters, the system employs a CRDNN encoder.
The decoder is based on a standard  GRU. Beamsearch coupled with a RNN
language model is used  on the top of decoder probabilities.

The neural network is trained on both CTC and negative-log likelihood
targets and sub-word units estimated with Byte Pairwise Encoding (BPE)
are used as basic recognition tokens. Training is performed on the full
LibriSpeech dataset (960 h).

The experiment file is flexible enough to support a large variety of
different systems. By properly changing the parameter files, you can try
different encoders, decoders, tokens (e.g, characters instead of BPE),
training split (e.g, train-clean 100 rather than the full one), and many
other possible variations.

This recipe assumes that the tokenizer and the LM are already trained.
To avoid token mismatches, the tokenizer used for the acoustic model is
the same use for the LM.  The recipe downloads the pre-trained tokenizer
and LM.

If you would like to train a full system from scratch do the following:
1- Train a tokenizer (see ../../Tokenizer)
2- Train a language model (see ../../LM)
3- Train the acoustic model (with this code).



Authors
 * Ju-Chieh Chou 2020
 * Mirco Ravanelli 2020
 * Abdel Heba 2020
 * Peter Plantinga 2020
 * Samuele Cornell 2020
 * Andreas Nautsch 2021
"""

import os
import sys
import torch
import logging

# Speechbrain path
sys.path.append("../../../../")
import speechbrain as sb
from speechbrain.utils.distributed import run_on_main
from hyperpyyaml import load_hyperpyyaml
from pathlib import Path

from transformers import GPT2LMHeadModel, GPT2Tokenizer

sys.path.append("../../../LibriSpeech/ASR/seq2seq")

logger = logging.getLogger(__name__)


# Define training procedure
class ASR(sb.Brain):
    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        tokens_bos, _ = batch.tokens_bos
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)

        # Forward pass
        feats = self.modules.wav2vec2(wavs)
        #feats = torch.nn.functional.avg_pool1d(feats.transpose(1, 2), kernel_size=2).transpose(1, 2)

        e_in = self.modules.emb(tokens_bos)  # y_in bos + tokens
        h, _ = self.modules.dec(e_in, feats, wav_lens)

        # Output layer for seq2seq log-probabilities
        logits = self.modules.seq_lin(h)
        p_seq = self.hparams.log_softmax(logits)

        # Compute outputs
        if stage == sb.Stage.TRAIN:
            current_epoch = self.hparams.epoch_counter.current
            if current_epoch <= self.hparams.number_of_ctc_epochs:
                # Output layer for ctc log-probabilities
                logits = self.modules.ctc_lin(feats)
                p_ctc = self.hparams.log_softmax(logits)
                return p_ctc, p_seq, wav_lens
            else:
                return p_seq, wav_lens
        else:
            p_tokens, scores = self.hparams.search(feats, wav_lens)
            #p_tokens = p_tokens[0]
            #scores = scores[0]
            #print(f"{len(p_tokens), scores}")
            
            lmb = 0.01
            batch_best_tokens = []
            # github.com/huggingface/transformers/issues/1009
            # batch loop
            for j in range(len(scores)):
            #for token_candidates, score_candidates in zip(p_tokens, scores):
                best_score = None
                best_tokens = None
                # topk loop
                for k in range(len(p_tokens)):
                #for utt_seq, utt_score in zip(token_candidates, score_candidates):
                    utt_seq = p_tokens[k][j]
                    utt_score = scores[j][k]
                    decoded = "".join(self.tokenizer.decode_ndim(utt_seq)).lower()
                    #print(decoded)
                    input_ids = torch.tensor(gpt_tokenizer.encode(decoded)).unsqueeze(0)
                    tokenize_input = gpt_tokenizer.tokenize(decoded)
                    tensor_input = torch.tensor([[gpt_tokenizer.eos_token_id] + gpt_tokenizer.convert_tokens_to_ids(tokenize_input)])
                    with torch.no_grad():
                        outputs = gpt_model(tensor_input, labels=tensor_input)
                        loss, logits = outputs[:2]
                    
                    lp = 0.0
                    for i in range(len(tokenize_input)):
                        masked_index = i
                        predicted_score = logits[0, masked_index]
                        predicted_prob = self.hparams.log_softmax(predicted_score)
                        lp += predicted_prob[gpt_tokenizer.convert_tokens_to_ids([tokenize_input[i]])[0]]

                    fusion_score = utt_score + lmb * lp
                    if best_score is None or fusion_score > best_score:
                        best_score = fusion_score
                        best_tokens = utt_seq
                    #print(f'Decoded: {decoded}')
                    #print(f"b: {lp}")
 
                batch_best_tokens.append(best_tokens)
                #print(f'Best tokens: {"".join(self.tokenizer.decode_ndim(best_tokens))}')
            return p_seq, wav_lens, batch_best_tokens

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss (CTC+NLL) given predictions and targets."""

        if stage == sb.Stage.TRAIN:
            current_epoch = self.hparams.epoch_counter.current
            if current_epoch <= self.hparams.number_of_ctc_epochs:
                p_ctc, p_seq, wav_lens = predictions
            else:
                p_seq, wav_lens = predictions
        else:
            p_seq, wav_lens, predicted_tokens = predictions

        ids = batch.id
        tokens_eos, tokens_eos_lens = batch.tokens_eos
        tokens, tokens_lens = batch.tokens

        loss_seq = self.hparams.seq_cost(
            p_seq, tokens_eos, length=tokens_eos_lens
        )

        # Add ctc loss if necessary
        if (
            stage == sb.Stage.TRAIN
            and current_epoch <= self.hparams.number_of_ctc_epochs
        ):
            loss_ctc = self.hparams.ctc_cost(
                p_ctc, tokens, wav_lens, tokens_lens
            )
            loss = self.hparams.ctc_weight * loss_ctc
            loss += (1 - self.hparams.ctc_weight) * loss_seq
        else:
            loss = loss_seq

        if stage != sb.Stage.TRAIN:
            # Decode token terms to words
            predicted_words = [
                "".join(self.tokenizer.decode_ndim(utt_seq)).split(" ")
                for utt_seq in predicted_tokens
            ]
            target_words = [wrd.split(" ") for wrd in batch.wrd]
            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        predictions = self.compute_forward(batch, sb.Stage.TRAIN)
        loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)
        loss.backward()
        if self.check_gradients(loss):
            self.optimizer.step()
        self.optimizer.zero_grad()
        return loss.detach()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        predictions = self.compute_forward(batch, stage=stage)
        with torch.no_grad():
            loss = self.compute_objectives(predictions, batch, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.error_rate_computer()

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of a epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr, new_lr = self.hparams.lr_annealing(stage_stats["WER"])
            sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)
            self.hparams.train_logger.log_stats(
                stats_meta={"epoch": epoch, "lr": old_lr},
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w") as w:
                self.wer_metric.write_stats(w)


def dataio_prepare(hparams):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""
    data_folder = hparams["data_folder"]

    train_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["train_csv"], replacements={"data_root": data_folder},
    )

    if hparams["sorting"] == "ascending":
        # we sort training data to speed up training and get better results.
        train_data = train_data.filtered_sorted(sort_key="duration")
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["train_dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "descending":
        train_data = train_data.filtered_sorted(
            sort_key="duration", reverse=True
        )
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["train_dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "sorting must be random, ascending or descending"
        )

    valid_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["valid_csv"], replacements={"data_root": data_folder},
    )
    valid_data = valid_data.filtered_sorted(sort_key="duration")

    # test is separate
    test_datasets = {}
    for csv_file in hparams["test_csv"]:
        name = Path(csv_file).stem
        test_datasets[name] = sb.dataio.dataset.DynamicItemDataset.from_csv(
            csv_path=csv_file, replacements={"data_root": data_folder}
        )
        test_datasets[name] = test_datasets[name].filtered_sorted(
            sort_key="duration"
        )

    datasets = [train_data, valid_data] + [i for k, i in test_datasets.items()]

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("wav")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(wav):
        sig = sb.dataio.dataio.read_audio(wav)
        return sig

    sb.dataio.dataset.add_dynamic_item(datasets, audio_pipeline)
    label_encoder = sb.dataio.encoder.CTCTextEncoder()

    # 3. Define text pipeline:
    @sb.utils.data_pipeline.takes("wrd")
    @sb.utils.data_pipeline.provides(
        "wrd", "char_list", "tokens_list", "tokens_bos", "tokens_eos", "tokens"
    )
    def text_pipeline(wrd):
        yield wrd
        char_list = list(wrd)
        yield char_list
        tokens_list = label_encoder.encode_sequence(char_list)
        yield tokens_list
        tokens_bos = torch.LongTensor([hparams["bos_index"]] + (tokens_list))
        yield tokens_bos
        tokens_eos = torch.LongTensor(tokens_list + [hparams["eos_index"]])
        yield tokens_eos
        tokens = torch.LongTensor(tokens_list)
        yield tokens

    sb.dataio.dataset.add_dynamic_item(datasets, text_pipeline)

    lab_enc_file = os.path.join(hparams["save_folder"], "label_encoder.txt")
    special_labels = {
        "bos_label": hparams["bos_index"],
        "eos_label": hparams["eos_index"],
        "blank_label": hparams["blank_index"],
    }
    label_encoder.load_or_create(
        path=lab_enc_file,
        from_didatasets=[train_data],
        output_key="char_list",
        special_labels=special_labels,
        sequence_input=True,
    )
    # 4. Set output:
    sb.dataio.dataset.set_output_keys(
        datasets, ["id", "sig", "wrd", "char_list", "tokens_bos", "tokens_eos", "tokens"],
    )

    return (
        train_data,
        valid_data,
        test_datasets,
        label_encoder,
    )


if __name__ == "__main__":

    # Set up GPT
    gpt_model = GPT2LMHeadModel.from_pretrained("gpt2")
    gpt_tokenizer = GPT2Tokenizer.from_pretrained("gpt2")

    # CLI:
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    # If distributed_launch=True then
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Dataset prep (parsing Librispeech)
    from librispeech_prepare import prepare_librispeech  # noqa

    # here we create the datasets objects as well as tokenization and encoding
    (
        train_data,
        valid_data,
        test_datasets,
        label_encoder,
    ) = dataio_prepare(hparams)

    # Trainer initialization
    asr_brain = ASR(
        modules=hparams["modules"],
        opt_class=hparams["opt_class"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    # We dynamicaly add the tokenizer to our brain class.
    # NB: This tokenizer corresponds to the one used for the LM!!
    train_dataloader_opts = hparams["train_dataloader_opts"]
    valid_dataloader_opts = hparams["valid_dataloader_opts"]

    # We dynamicaly add the tokenizer to our brain class.
    asr_brain.tokenizer = label_encoder

    # Training
    asr_brain.fit(
        asr_brain.hparams.epoch_counter,
        train_data,
        valid_data,
        train_loader_kwargs=train_dataloader_opts,
        valid_loader_kwargs=valid_dataloader_opts,
    )

    # Testing
    for k in test_datasets.keys():  # keys are test_clean, test_other etc
        asr_brain.hparams.wer_file = os.path.join(
            hparams["output_folder"], "wer_{}.txt".format(k)
        )
        asr_brain.evaluate(
            test_datasets[k], test_loader_kwargs=hparams["test_dataloader_opts"]
        )
